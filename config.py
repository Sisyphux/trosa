"""
客户跟进提醒系统 - 配置文件
所有可配置项集中管理
"""
import os


def _positive_int_env(name, default, minimum=1):
    """Read a positive integer setting with an actionable configuration error."""
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'{name} must be an integer, got {raw!r}') from exc
    if value < minimum:
        raise RuntimeError(f'{name} must be at least {minimum}, got {value}')
    return value

# ========== 本地存储维护 ==========
# 仅清理可再生成的临时文件和超出保留策略的自动数据库快照。客户附件
# （uploads/customer_files/）是持久业务数据，不参与自动删除。手工命名的
# 特殊备份不参与自动删除，避免影响用户主动保留的恢复点。
STORAGE_MAINTENANCE_CONFIG = {
    'backup_retain_days': _positive_int_env('CRM_BACKUP_RETAIN_DAYS', 90),
    'recent_backup_snapshots_per_day': _positive_int_env('CRM_RECENT_BACKUPS_PER_DAY', 12),
    'older_backup_snapshots_per_day': _positive_int_env('CRM_OLDER_BACKUPS_PER_DAY', 3),
    'upload_retain_days': _positive_int_env('CRM_UPLOAD_RETAIN_DAYS', 45),
    'audit_temp_retain_days': _positive_int_env('CRM_AUDIT_TEMP_RETAIN_DAYS', 14),
}



# ========== 邮箱可发送性 SMTP 探测（默认关闭）==========
# 启用前必须配置可解析的 EHLO 主机名和可接收退信的专用 envelope sender。
EMAIL_VERIFICATION_CONFIG = {
    'smtp_probe_enabled': os.environ.get('EMAIL_VERIFY_SMTP_ENABLED', 'false').lower() == 'true',
    'smtp_timeout_seconds': _positive_int_env('EMAIL_VERIFY_SMTP_TIMEOUT', 8),
    'smtp_max_mx_attempts': _positive_int_env('EMAIL_VERIFY_SMTP_MAX_MX', 2),
    'smtp_helo_host': os.environ.get('EMAIL_VERIFY_HELO_HOST', ''),
    'smtp_mail_from': os.environ.get('EMAIL_VERIFY_MAIL_FROM', ''),
    'catchall_enabled': os.environ.get('EMAIL_VERIFY_CATCHALL_ENABLED', 'false').lower() == 'true',
    'catchall_secret': os.environ.get('EMAIL_VERIFY_CATCHALL_SECRET', ''),
    'domain_probe_cache_days': _positive_int_env('EMAIL_VERIFY_DOMAIN_CACHE_DAYS', 7),
    'job_interval_seconds': _positive_int_env('EMAIL_VERIFY_JOB_INTERVAL', 30),
}
