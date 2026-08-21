"""
定时任务模块 - 每天 Windows 系统通知提醒（多用户版）
使用 APScheduler 实现定时调度
"""
import os
import json
import logging
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from db import (
    get_db, get_system_db, set_db_user, USERS, USERS_LIST, DB_DIR,
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


def get_today_reminders():
    """获取所有用户的今日及逾期提醒"""
    today = datetime.now().strftime('%Y-%m-%d')
    all_reminders = []
    user_stats = {}
    
    for user in USERS:
        try:
            set_db_user(user)
            conn = get_db()
            c = conn.cursor()
            c.execute('''
                SELECT r.*, c.name as customer_name, c.company as customer_company, 
                       c.country, c.level, c.status, c.field
                FROM reminders r
                JOIN customers c ON r.customer_id = c.id
                WHERE r.is_done = 0 AND r.remind_date <= ?
                  AND (c.is_deleted = 0 OR c.is_deleted IS NULL)
                ORDER BY r.remind_date ASC, c.level DESC
            ''', (today,))
            user_reminders = [dict(row) for row in c.fetchall()]
            
            # Add user label to each reminder
            for r in user_reminders:
                r['user_label'] = USERS[user]['label']
                automatic = str(r.get('reminder_type') or '').startswith('outreach_')
                r['is_automatic_development'] = automatic
                r['reminder_category_label'] = '自动开发节点' if automatic else '人工跟进'
            
            c.execute('SELECT COUNT(*) FROM customers WHERE (is_deleted = 0 OR is_deleted IS NULL)')
            total = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM reminders WHERE is_done = 0 AND remind_date <= ?', (today,))
            pending = c.fetchone()[0]
            c.execute('SELECT COUNT(*) FROM reminders WHERE is_done = 0 AND remind_date < ?', (today,))
            overdue = c.fetchone()[0]
            conn.close()
            
            user_stats[user] = {
                'total_customers': total,
                'pending_reminders': pending,
                'overdue_reminders': overdue,
                'label': USERS[user]['label'],
            }
            all_reminders.extend(user_reminders)
        except Exception as e:
            logger.error(f'获取 {user} 的提醒失败: {e}')
    
    # Reset user context
    set_db_user(None)
    
    # Aggregate stats
    total_all = sum(s.get('total_customers', 0) for s in user_stats.values())
    pending_all = sum(s.get('pending_reminders', 0) for s in user_stats.values())
    overdue_all = sum(s.get('overdue_reminders', 0) for s in user_stats.values())
    
    stats = {
        'total_customers': total_all,
        'pending_reminders': pending_all,
        'overdue_reminders': overdue_all,
        'per_user': user_stats,
    }
    
    return all_reminders, stats



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


def send_windows_notification(title, body):
    """发送 Windows 系统通知（使用 PowerShell）"""
    escaped_title = title.replace("'", "''")
    escaped_body = body.replace("'", "''")
    
    ps_script = f'''
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $textNodes = $template.GetElementsByTagName("text")
    $textNodes.Item(0).AppendChild($template.CreateTextNode('{escaped_title}')) | Out-Null
    $textNodes.Item(1).AppendChild($template.CreateTextNode('{escaped_body}')) | Out-Null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("客户跟进提醒系统").Show($toast)
    '''
    
    try:
        subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_script],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        )
        return True
    except Exception:
        # 备用方案：用 msg 命令（Windows 经典弹窗）
        try:
            subprocess.run(['msg', '*', f'{title}\n{body}'], capture_output=True, timeout=5)
            return True
        except Exception:
            return False


def send_daily_notification():
    """每天定时发送 Windows 系统通知"""
    logger.info("执行每日待办提醒通知...")

    try:
        reminders, stats = get_today_reminders()

        if not reminders:
            logger.info("今日没有待办提醒，跳过通知")
            return

        overdue = stats.get('overdue_reminders', 0)
        pending = stats.get('pending_reminders', 0)
        
        # 统计每个用户
        per_user = stats.get('per_user', {})
        user_lines = []
        for uid, us in per_user.items():
            if us.get('pending_reminders', 0) > 0:
                user_lines.append(f"  {us['label']}: {us['pending_reminders']}待办({us['overdue_reminders']}逾期)")

        # 生成通知正文：最多列 5 个客户
        lines = [f'待办 {pending} 项（逾期 {overdue} 项）']
        if user_lines:
            lines.append('───')
            lines.extend(user_lines)
            lines.append('───')
        for r in reminders[:5]:
            name = r.get('customer_company') or r.get('customer_name', '未知')
            label = r.get('user_label', '')
            category = r.get('reminder_category_label', '人工跟进')
            prefix = f'[{category}] '
            lines.append(f'  [{label}] {prefix}{name}' if label else f'  - {prefix}{name}')
        if len(reminders) > 5:
            lines.append(f'  ... 还有 {len(reminders) - 5} 项')

        body = '\n'.join(lines)
        send_windows_notification('📋 客户跟进提醒', body)
        logger.info(f"✅ 系统通知已发送: {pending} 项待办, {overdue} 项逾期")

    except Exception as e:
        logger.error(f"通知发送异常: {str(e)}")


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

    # 每天早上 9:00 发送 Windows 系统通知
    trigger = CronTrigger(hour=9, minute=0, timezone=SCHEDULER_TIMEZONE)
    scheduler.add_job(
        send_daily_notification,
        trigger=trigger,
        id='daily_notification',
        name='每日跟进提醒通知',
        replace_existing=True
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

    # Website monitoring and AI research expiry jobs are intentionally frozen
    # in the Customer Memory scope.  Historical logs/reports remain readable,
    # while no new background work or reminders are created from them.

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

    scheduler.start()
    logger.info("✅ 定时调度器已启动，每天 02:15 本机快照、09:00 系统通知提醒")


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
