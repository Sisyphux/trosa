"""定时任务模块：备份与可选的后台同步任务。"""
import os
import json
import logging
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from db import (
    get_system_db, set_db_user, USERS,
    run_scheduled_local_backup,
)

logger = logging.getLogger(__name__)
scheduler = None
SCHEDULER_TIMEZONE = ZoneInfo('Asia/Shanghai')


def _user_module_enabled(user: str, module: str) -> bool:
    """Read the per-user module switch without importing the Flask app."""
    try:
        conn = get_system_db()
        row = conn.execute('SELECT value FROM app_settings WHERE key=?', (f'user_preferences:{user}',)).fetchone()
        conn.close()
        if not row or not row['value']:
            return True
        preferences = json.loads(row['value'])
        return (preferences.get('modules') or {}).get(module, True) is not False
    except Exception as exc:
        logger.warning(f'读取 {user} 模块设置失败，继续保持模块运行: {exc}')
        return True


def _run_email_verification_jobs():
    """Process a small per-user batch so SMTP timeouts never block Flask requests."""
    from email_verifier import is_configured, process_pending_email_verification_jobs

    if not is_configured():
        return
    for user in USERS:
        if not _user_module_enabled(user, 'email_validation'):
            continue
        try:
            set_db_user(user)
            result = process_pending_email_verification_jobs(max_jobs=3)
            if result.get('processed'):
                logger.info(f'邮箱 SMTP 验证 [{user}] 完成 {result["processed"]} 项')
        except Exception as exc:
            logger.error(f'邮箱 SMTP 验证 [{user}] 失败: {exc}')
    set_db_user(None)


def _run_gmail_sync_jobs():
    """Kick off bounded, account-scoped Gmail sync workers without blocking APScheduler."""
    try:
        from gmail_sync import enqueue_scheduled_gmail_sync
        enqueue_scheduled_gmail_sync()
    except Exception:
        logger.exception('Gmail 定时同步调度失败')


def _run_local_backup():
    """Run the daily local snapshot without changing the active database."""
    try:
        run_scheduled_local_backup()
    except Exception:
        # A backup failure must be visible in logs while leaving the CRM
        # request process available for reads and an explicit retry.
        logger.exception('每日本机备份任务异常')


def start_scheduler():
    """启动定时调度器"""
    global scheduler

    if scheduler is not None and scheduler.running:
        logger.info("定时调度器已在运行")
        return

    scheduler = BackgroundScheduler(
        timezone=SCHEDULER_TIMEZONE,
        job_defaults={'coalesce': True, 'max_instances': 1, 'misfire_grace_time': 3600},
    )

    # Keep a recovery point even on days with no edits.  A long misfire grace
    # period lets a launchd restart catch up after a short host outage without
    # introducing a second database writer or an automatic failover path.
    scheduler.add_job(
        _run_local_backup,
        trigger=CronTrigger(hour=2, minute=15, timezone=SCHEDULER_TIMEZONE),
        id='local_backup_daily',
        name='每日本机数据库快照',
        replace_existing=True,
        misfire_grace_time=7 * 24 * 60 * 60,
    )

    from config import EMAIL_VERIFICATION_CONFIG
    if EMAIL_VERIFICATION_CONFIG.get('smtp_probe_enabled'):
        scheduler.add_job(
            func=_run_email_verification_jobs,
            trigger='interval',
            seconds=max(10, int(EMAIL_VERIFICATION_CONFIG.get('job_interval_seconds', 30))),
            id='email_verification_worker',
            name='邮箱 SMTP 可发送性验证',
            replace_existing=True,
            max_instances=1,
        )

    # Gmail is optional and is registered only after OAuth credentials and a
    # token-encryption secret are present. Connected accounts are still
    # checked individually inside the worker, so one user never unlocks or
    # reads another user's mailbox.
    try:
        from gmail_sync import scheduler_enabled
        gmail_enabled = scheduler_enabled()
    except Exception:
        gmail_enabled = False
        logger.exception('Gmail 同步配置检查失败')
    if gmail_enabled:
        try:
            gmail_interval = max(60, min(int(os.environ.get('GMAIL_SYNC_INTERVAL_SECONDS', '300')), 3600))
        except (TypeError, ValueError):
            gmail_interval = 300
        scheduler.add_job(
            func=_run_gmail_sync_jobs,
            trigger='interval',
            seconds=gmail_interval,
            id='gmail_sync_worker',
            name='Gmail 沟通增量同步',
            replace_existing=True,
            max_instances=1,
        )

    scheduler.start()
    logger.info("✅ 定时调度器已启动，每天 02:15 本机快照")


def stop_scheduler():
    """停止定时调度器"""
    global scheduler
    if scheduler is not None and scheduler.running:
        scheduler.shutdown()
        logger.info("定时调度器已停止")


def get_scheduler_status():
    """获取调度器状态"""
    if scheduler is None:
        return {'running': False, 'jobs': []}

    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            'id': job.id,
            'name': job.name,
            'next_run_time': str(job.next_run_time) if job.next_run_time else None,
        })

    return {
        'running': scheduler.running,
        'jobs': jobs,
    }
