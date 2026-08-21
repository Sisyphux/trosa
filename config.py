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

# ========== SMTP 邮件配置（已禁用）==========
# 由于 SSL 连接不稳定 (EOF occurred in violation of protocol)，邮件功能已禁用。
# 如需重新启用：1) 设置 enabled=True  2) 填写正确的邮箱账号密码
SMTP_CONFIG = {
    'enabled': False,
    'host': 'smtp.qiye.163.com',
    'port': 465,
    'user': 'hamid.luo@hzrj-intl.com',
    'password': '',
    'use_tls': True,
    'to_email': 'hamid.luo@hzrj-intl.com',
}

# 从环境变量读取配置（优先级高于上面的默认值）
if os.environ.get('SMTP_HOST'):
    SMTP_CONFIG['host'] = os.environ.get('SMTP_HOST')
    SMTP_CONFIG['port'] = _positive_int_env('SMTP_PORT', 587)
    SMTP_CONFIG['user'] = os.environ.get('SMTP_USER', '')
    SMTP_CONFIG['password'] = os.environ.get('SMTP_PASSWORD', '')
    SMTP_CONFIG['use_tls'] = os.environ.get('SMTP_TLS', 'true').lower() == 'true'
    SMTP_CONFIG['to_email'] = os.environ.get('SMTP_TO_EMAIL', SMTP_CONFIG['user'])
    SMTP_CONFIG['enabled'] = os.environ.get('SMTP_ENABLED', 'false').lower() == 'true'


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


# ========== Excel 同步配置 ==========
EXCEL_CONFIG = {
    'filename': 'Hamid客户跟进表格.xlsx',
    'sheet_name': '中东客户跟进',  # Excel 中的 sheet 名称
    'enable_bidirectional_sync': False,  # 双向同步已禁用（会破坏Excel格式）
    'auto_remove_orphans': False,  # 自动删除数据库中已从Excel删除的客户（True=自动清理，False=保留不动）
    # 注意：已改为 False，防止系统自动删除用户手动添加的客户。
    # Excel 同步时不再自动清理，改为在界面的回收站中手动管理。
}

# ========== 官网监控配置 ==========
WEB_MONITOR_CONFIG = {
    'enabled': True,                    # 是否启用官网监控
    'change_threshold': 0.8,            # 内容相似度阈值（低于此值视为变化）
    'request_timeout': 15,              # 官网请求超时（秒）
    'research_expiry_days': 30,         # 背调报告过期天数
    # 分层监控频率（按客户等级）
    'frequency': {
        'A': 'every_4h',               # 核心客户每 4 小时
        'B': 'every_4h',               # 重要客户每 4 小时
        'C+': 'daily',                 # 潜力客户每天
        'C': 'weekly',                 # 常规客户每周
        'D': 'disabled',               # 不监控
    },
    'max_changes_per_day': 10,          # 每天最多生成多少条变化提醒
}

# ========== LLM 配置 ==========
LLM_CONFIG = {
    'backend': 'deepseek',              # deepseek(推荐) / qwen / glm / openai / lmstudio / ollama / auto
    'research_model': 'deepseek-chat',  # 背调报告生成模型
    'research_max_tokens': 3072,        # 背调报告生成最大 token
    'research_temperature': 0.3,        # 温度参数
}
