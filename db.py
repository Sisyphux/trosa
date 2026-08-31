"""
客户跟进提醒系统 - 数据库模型与初始化（多用户版）
SQLite 本地存储，每人独立数据库，支持自动备份与恢复
"""
import sqlite3
import sys
import os
import platform
import logging
import shutil
import json
import hashlib
import time
import uuid
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ========== 用户配置 ==========
USERS = {
    'hamid': {'name': 'Hamid', 'label': 'Hamid', 'color': '#8B9DAF'},
    'amy':   {'name': 'Amy',   'label': 'Amy',   'color': '#C4877A'},
    'kelley': {'name': 'Kelley', 'label': 'Kelley', 'color': '#8BA88A'},
}
USERS_LIST = list(USERS.keys())

import threading
from config import STORAGE_MAINTENANCE_CONFIG

# ========== 当前用户上下文（线程安全） ==========
_thread_local = threading.local()
_journal_mode_lock = threading.Lock()
_journal_mode_ready = set()

def set_db_user(user):
    """设置当前线程的数据库用户（用于 get_db() 路由）"""
    _thread_local.current_user = user

def get_current_user():
    """获取当前线程的用户"""
    return getattr(_thread_local, 'current_user', None)


def _configure_journal_mode(conn, path):
    """在进程内只尝试一次 WAL，避免每个请求都触发 SQLite 文件锁。"""
    normalized_path = os.path.realpath(path)
    with _journal_mode_lock:
        if normalized_path in _journal_mode_ready:
            return
        try:
            current_mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
            if str(current_mode).lower() != 'wal':
                conn.execute('PRAGMA journal_mode = WAL')
        except sqlite3.DatabaseError as exc:
            # 只读目录、同步软件短暂占用或旧版 Windows 文件锁不应阻断读取请求。
            # 写入操作仍会得到自己的明确错误；后续进程启动时会再次尝试开启 WAL。
            logger.warning('SQLite WAL 不可用，继续使用当前日志模式 [%s]: %s', path, exc)
        finally:
            _journal_mode_ready.add(normalized_path)


# ========== 路径工具 ==========

def is_packaged():
    """检测是否运行在 PyInstaller 打包环境中"""
    return getattr(sys, 'frozen', False)


def get_app_root():
    """获取应用根目录"""
    if is_packaged():
        system = platform.system()
        if system == 'Darwin':
            return os.path.expanduser('~/Library/Application Support/客户跟进提醒系统')
        elif system == 'Windows':
            return os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), '客户跟进提醒系统')
        else:
            return os.path.join(os.path.expanduser('~'), '.crm_reminders')
    else:
        module_root = os.path.dirname(os.path.abspath(__file__))
        parent_root = os.path.dirname(module_root)
        # Extracted archives may contain ``CRM reminder/CRM reminder``. Always
        # converge on the outer source tree when it contains the real entrypoints.
        if (os.path.basename(module_root).casefold() == os.path.basename(parent_root).casefold()
                and os.path.isfile(os.path.join(parent_root, 'app.py'))
                and os.path.isfile(os.path.join(parent_root, 'db.py'))):
            return parent_root
        return module_root


def get_db_dir():
    """获取数据库目录"""
    env_db_path = os.environ.get('CRM_DB_PATH')
    if env_db_path:
        return env_db_path
    return os.path.join(get_app_root(), 'data')


# 数据库目录
DB_DIR = os.path.realpath(os.path.abspath(get_db_dir()))


def ensure_db_identity():
    """Write an auditable marker for the one active data store used by this installation."""
    ensure_db_dir()
    marker_path = os.path.join(DB_DIR, 'active_store.json')
    marker = {}
    try:
        if os.path.exists(marker_path):
            with open(marker_path, 'r', encoding='utf-8') as handle:
                marker = json.load(handle)
    except Exception:
        marker = {}
    marker.update({
        'store_id': marker.get('store_id') or str(uuid.uuid4()),
        'db_dir': DB_DIR,
        'app_root': get_app_root(),
        'last_started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'users': sorted(USERS.keys()),
    })
    temporary = marker_path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(marker, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, marker_path)
    return marker


def ensure_db_dir():
    """确保数据库目录存在"""
    os.makedirs(DB_DIR, exist_ok=True)
    # 确保备份目录存在
    os.makedirs(os.path.join(DB_DIR, 'backups'), exist_ok=True)


# ========== 数据库连接 ==========

def get_system_db():
    """获取系统数据库连接（存放用户元数据、周报等）"""
    ensure_db_dir()
    path = os.path.join(DB_DIR, 'system.db')
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    _configure_journal_mode(conn, path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA cache_size = -2000")
    except sqlite3.DatabaseError as exc:
        logger.warning('SQLite 连接参数未完全应用 [%s]，继续提供读取服务: %s', path, exc)
    return conn


def get_user_db_path(user):
    """获取用户数据库文件路径"""
    if user in USERS:
        return os.path.join(DB_DIR, f'{user}.db')
    return os.path.join(DB_DIR, 'system.db')


def get_db():
    """获取当前用户的数据库连接（根据线程上下文自动路由）"""
    ensure_db_dir()
    user = get_current_user()
    if user and user in USERS:
        path = get_user_db_path(user)
    else:
        path = os.path.join(DB_DIR, 'system.db')
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    _configure_journal_mode(conn, path)
    conn.execute("PRAGMA foreign_keys = ON")
    # CRM data favours durable commits over marginally faster writes.
    try:
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA cache_size = -4000")
    except sqlite3.DatabaseError as exc:
        logger.warning('SQLite 连接参数未完全应用 [%s]，继续提供读取服务: %s', path, exc)
    return conn


# ========== 数据库完整性检查 ==========

def check_integrity():
    """检查所有数据库的完整性，返回检查结果"""
    results = {}
    ensure_db_dir()
    
    all_dbs = {'system.db': os.path.join(DB_DIR, 'system.db')}
    for user in USERS:
        all_dbs[f'{user}.db'] = get_user_db_path(user)
    
    for name, path in all_dbs.items():
        try:
            if os.path.exists(path):
                conn = sqlite3.connect(path, timeout=10.0)
                c = conn.cursor()
                c.execute("PRAGMA integrity_check")
                result = c.fetchone()[0]
                conn.close()
                results[name] = 'ok' if result == 'ok' else result
            else:
                results[name] = 'not_found'
        except Exception as e:
            results[name] = f'error: {e}'
    
    return results


# ========== 自动备份 ==========

def _file_checksum(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_snapshot(source_path, destination_path):
    """Copy a WAL-mode SQLite database as one consistent snapshot."""
    temporary_path = destination_path + '.tmp'
    if os.path.exists(temporary_path):
        os.remove(temporary_path)
    source = sqlite3.connect(source_path, timeout=30.0)
    destination = sqlite3.connect(temporary_path, timeout=30.0)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    os.replace(temporary_path, destination_path)


def backup_database(reason='manual'):
    """Create a timestamped, checksummed snapshot of databases and CRM attachments."""
    ensure_db_dir()
    now = datetime.now()
    date_str = now.strftime('%Y-%m-%d')
    backup_dir = os.path.join(DB_DIR, 'backups', date_str, now.strftime('%H%M%S_%f'))
    os.makedirs(backup_dir, exist_ok=True)

    backed_up = []
    failed = []
    store_id = ''
    try:
        with open(os.path.join(DB_DIR, 'active_store.json'), 'r', encoding='utf-8') as handle:
            store_id = json.load(handle).get('store_id', '')
    except Exception:
        pass
    manifest = {
        'created_at': now.strftime('%Y-%m-%d %H:%M:%S'), 'reason': reason,
        'store_id': store_id, 'db_dir': DB_DIR, 'files': [], 'attachments': []
    }
    databases = {'system.db': os.path.join(DB_DIR, 'system.db')}
    databases.update({f'{user}.db': get_user_db_path(user) for user in USERS})

    for name, source_path in databases.items():
        if not os.path.exists(source_path):
            continue
        destination_path = os.path.join(backup_dir, name)
        try:
            _sqlite_snapshot(source_path, destination_path)
            manifest['files'].append({
                'kind': 'database',
                'name': name,
                'bytes': os.path.getsize(destination_path),
                'sha256': _file_checksum(destination_path),
            })
            backed_up.append(name)
        except Exception as e:
            failed.append(f'{name}: {e}')

    # Attachments are durable CRM data, so keep them beside the database
    # snapshots and checksum every file for restore-time verification.
    attachment_root = os.path.join(DB_DIR, 'uploads', 'customer_files')
    backup_attachment_root = os.path.join(backup_dir, 'uploads', 'customer_files')
    if os.path.isdir(attachment_root):
        try:
            for root, _, names in os.walk(attachment_root):
                for name in names:
                    source_path = os.path.join(root, name)
                    relative = os.path.relpath(source_path, attachment_root)
                    destination_path = os.path.join(backup_attachment_root, relative)
                    os.makedirs(os.path.dirname(destination_path), exist_ok=True)
                    shutil.copy2(source_path, destination_path)
                    manifest['attachments'].append({
                        'name': os.path.join('uploads', 'customer_files', relative),
                        'bytes': os.path.getsize(destination_path),
                        'sha256': _file_checksum(destination_path),
                    })
        except Exception as e:
            failed.append(f'attachments: {e}')

    manifest['failed'] = failed
    with open(os.path.join(backup_dir, 'manifest.json'), 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    if not failed:
        _cleanup_old_backups()
    return {'backed_up': backed_up, 'failed': failed, 'path': backup_dir}


_safety_backup_lock = threading.Lock()
_safety_backup_timer = None
_last_safety_backup_at = 0.0
_restore_lock = threading.Lock()


def schedule_safety_backup(reason='data_change'):
    """Debounce automatic snapshots so normal editing remains responsive."""
    global _safety_backup_timer, _last_safety_backup_at
    with _safety_backup_lock:
        if _safety_backup_timer is not None:
            return
        elapsed = time.time() - _last_safety_backup_at
        delay = 3 if elapsed >= 20 else max(1, 20 - elapsed)

        def run_backup():
            global _safety_backup_timer, _last_safety_backup_at
            try:
                backup_database(reason)
                _last_safety_backup_at = time.time()
            except Exception as e:
                logger.error(f'自动安全备份失败: {e}')
            finally:
                with _safety_backup_lock:
                    _safety_backup_timer = None

        _safety_backup_timer = threading.Timer(delay, run_backup)
        _safety_backup_timer.daemon = True
        _safety_backup_timer.start()


def _cleanup_old_backups(retain_days=None):
    """Retain recoverable automatic snapshots while capping high-frequency copies."""
    backup_root = os.path.join(DB_DIR, 'backups')
    if not os.path.exists(backup_root):
        return {'removed_directories': 0, 'removed_bytes': 0}
    retain_days = retain_days or STORAGE_MAINTENANCE_CONFIG['backup_retain_days']
    recent_limit = max(1, STORAGE_MAINTENANCE_CONFIG['recent_backup_snapshots_per_day'])
    older_limit = max(1, STORAGE_MAINTENANCE_CONFIG['older_backup_snapshots_per_day'])
    cutoff = datetime.now() - timedelta(days=retain_days)
    removed_directories = 0
    removed_bytes = 0

    def remove_snapshot(path):
        nonlocal removed_directories, removed_bytes
        size = 0
        for root, _, files in os.walk(path):
            for name in files:
                try:
                    size += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        shutil.rmtree(path)
        removed_directories += 1
        removed_bytes += size

    for d in os.listdir(backup_root):
        d_path = os.path.join(backup_root, d)
        if os.path.isdir(d_path):
            try:
                d_date = datetime.strptime(d, '%Y-%m-%d')
                if d_date < cutoff:
                    remove_snapshot(d_path)
                    logger.info(f'已清理旧备份: {d}')
                    continue
                age_days = max(0, (datetime.now().date() - d_date.date()).days)
                routine_limit = recent_limit if age_days <= 7 else older_limit
                routine_kept = 0
                for snapshot in sorted(os.listdir(d_path), reverse=True):
                    snapshot_path = os.path.join(d_path, snapshot)
                    if not os.path.isdir(snapshot_path):
                        continue
                    reason = ''
                    try:
                        with open(os.path.join(snapshot_path, 'manifest.json'), 'r', encoding='utf-8') as handle:
                            reason = json.load(handle).get('reason', '')
                    except Exception:
                        pass
                    if reason and reason not in ('data_change', 'startup', 'scheduled_local'):
                        continue
                    routine_kept += 1
                    if routine_kept > routine_limit:
                        remove_snapshot(snapshot_path)
            except ValueError:
                continue
    return {'removed_directories': removed_directories, 'removed_bytes': removed_bytes}


def run_startup_maintenance():
    """Perform conservative storage housekeeping once per application launch."""
    ensure_db_dir()
    result = _cleanup_old_backups()
    uploads_root = os.path.join(DB_DIR, 'uploads')
    uploads_cutoff = time.time() - STORAGE_MAINTENANCE_CONFIG['upload_retain_days'] * 86400
    if os.path.isdir(uploads_root):
        for root, directories, files in os.walk(uploads_root):
            # Customer attachments are durable CRM data.  They have database
            # metadata and must remain available regardless of upload retention.
            if root == uploads_root and 'customer_files' in directories:
                directories.remove('customer_files')
            for name in files:
                path = os.path.join(root, name)
                # Authoritative source files are user-owned data, never cleanup targets.
                if name.startswith('authoritative_'):
                    continue
                try:
                    if os.path.getmtime(path) < uploads_cutoff:
                        result['removed_bytes'] += os.path.getsize(path)
                        os.remove(path)
                        result['removed_files'] = result.get('removed_files', 0) + 1
                except OSError:
                    continue
    # This directory is created only by the spreadsheet visual-audit tool. It
    # never contains CRM data, but can retain a large node_modules tree.
    audit_temp = os.path.join(get_app_root(), 'tmp', 'spreadsheet_audit')
    audit_cutoff = time.time() - STORAGE_MAINTENANCE_CONFIG['audit_temp_retain_days'] * 86400
    audit_marker = os.path.join(audit_temp, 'audit_workbook.mjs')
    try:
        if os.path.isfile(audit_marker) and os.path.getmtime(audit_temp) < audit_cutoff:
            for root, _, files in os.walk(audit_temp):
                for name in files:
                    result['removed_bytes'] += os.path.getsize(os.path.join(root, name))
            shutil.rmtree(audit_temp)
            result['removed_directories'] += 1
    except OSError:
        logger.warning('无法清理过期表格审计临时目录', exc_info=True)
    logger.info('启动存储维护完成：删除 %d 个备份目录、%d 个过期上传，释放 %.1f MB',
                result['removed_directories'], result.get('removed_files', 0),
                result['removed_bytes'] / 1024 / 1024)
    return result


def run_scheduled_local_backup():
    """Create the daily local recovery point, independent of user writes.

    The application keeps one writable SQLite store.  This job only creates a
    versioned snapshot beside it; it never changes the active store or starts
    a second writer.  Serialising with restore prevents a scheduled snapshot
    from observing a partially restored data directory.
    """
    with _restore_lock:
        result = backup_database('scheduled_local')
    if result.get('failed'):
        logger.error('定时本机备份失败: %s', result['failed'])
    else:
        logger.info('定时本机备份完成: %s', result.get('path', ''))
    return result


# ========== 恢复备份 ==========

def restore_from_backup(backup_date):
    """Restore one checksummed database and attachment snapshot."""
    normalized = str(backup_date).replace('\\', '/').strip('/')
    parts = normalized.split('/')
    if not normalized or '..' in parts or any(not part for part in parts):
        return {'success': False, 'error': 'Invalid backup path'}
    backup_root = os.path.realpath(os.path.join(DB_DIR, 'backups'))
    backup_dir = os.path.realpath(os.path.join(backup_root, *parts))
    if os.path.commonpath((backup_root, backup_dir)) != backup_root or not os.path.isdir(backup_dir):
        return {'success': False, 'error': f'Backup directory not found: {backup_date}'}
    try:
        with open(os.path.join(backup_dir, 'manifest.json'), 'r', encoding='utf-8') as handle:
            manifest = json.load(handle)
        entries = manifest.get('files') or []
        has_attachment_manifest = 'attachments' in manifest
        attachment_entries = manifest.get('attachments') or []
    except (OSError, ValueError, TypeError) as exc:
        return {'success': False, 'error': f'Backup manifest is invalid: {exc}'}
    allowed = {'system.db', *(f'{user}.db' for user in USERS)}
    files = []
    for entry in entries:
        name = entry.get('name') if isinstance(entry, dict) else ''
        source_path = os.path.join(backup_dir, name)
        if name not in allowed or not os.path.isfile(source_path):
            return {'success': False, 'error': f'Backup manifest contains an invalid file: {name}'}
        if entry.get('sha256') != _file_checksum(source_path):
            return {'success': False, 'error': f'Backup checksum mismatch: {name}'}
        try:
            source = sqlite3.connect(source_path)
            try:
                check = source.execute('PRAGMA integrity_check').fetchone()[0]
            finally:
                source.close()
        except sqlite3.Error as exc:
            return {'success': False, 'error': f'Backup database cannot be read: {name}: {exc}'}
        if check != 'ok':
            return {'success': False, 'error': f'Backup integrity check failed: {name}: {check}'}
        files.append((name, source_path))
    attachments = []
    attachment_backup_root = os.path.realpath(os.path.join(backup_dir, 'uploads', 'customer_files'))
    for entry in attachment_entries:
        name = entry.get('name') if isinstance(entry, dict) else ''
        source_path = os.path.realpath(os.path.join(backup_dir, name))
        if (not name or not name.startswith('uploads/customer_files/')
                or os.path.commonpath((attachment_backup_root, source_path)) != attachment_backup_root
                or not os.path.isfile(source_path)):
            return {'success': False, 'error': f'Backup manifest contains an invalid attachment: {name}'}
        if entry.get('sha256') != _file_checksum(source_path):
            return {'success': False, 'error': f'Backup attachment checksum mismatch: {name}'}
        attachments.append((name, source_path))
    if not files:
        return {'success': False, 'error': 'Backup manifest contains no restorable databases'}
    with _restore_lock:
        safety_snapshot = backup_database('before_restore')
        if safety_snapshot.get('failed'):
            return {'success': False, 'error': f'Restore safety backup failed: {safety_snapshot["failed"]}'}
        restored = []
        for name, source_path in files:
            destination_path = os.path.join(DB_DIR, name)
            temporary_path = destination_path + '.restore.tmp'
            try:
                _sqlite_snapshot(source_path, temporary_path)
                for suffix in ('-wal', '-shm'):
                    sidecar = destination_path + suffix
                    if os.path.exists(sidecar):
                        os.remove(sidecar)
                os.replace(temporary_path, destination_path)
                restored.append(name)
            except (OSError, sqlite3.Error) as exc:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)
                return {'success': False, 'error': f'Restore failed for {name}: {exc}', 'restored': restored,
                        'safety_backup': safety_snapshot.get('path', '')}
        # Restore the complete attachment tree so files removed after the
        # snapshot do not remain visible as untracked data.
        if has_attachment_manifest:
            current_attachment_root = os.path.join(DB_DIR, 'uploads', 'customer_files')
            if os.path.isdir(current_attachment_root):
                shutil.rmtree(current_attachment_root)
            for name, source_path in attachments:
                relative = os.path.relpath(source_path, attachment_backup_root)
                destination_path = os.path.join(current_attachment_root, relative)
                os.makedirs(os.path.dirname(destination_path), exist_ok=True)
                shutil.copy2(source_path, destination_path)
            restored.append('uploads/customer_files')
    return {'success': True, 'restored': restored, 'attachments': len(attachments),
            'safety_backup': safety_snapshot.get('path', '')}


def list_backups():
    """List only snapshots that include a readable integrity manifest."""
    backup_root = os.path.join(DB_DIR, 'backups')
    if not os.path.isdir(backup_root):
        return []
    backups = []
    for date_name in sorted(os.listdir(backup_root), reverse=True):
        date_path = os.path.join(backup_root, date_name)
        if not os.path.isdir(date_path):
            continue
        for snapshot_name in sorted(os.listdir(date_path), reverse=True):
            snapshot_path = os.path.join(date_path, snapshot_name)
            manifest_path = os.path.join(snapshot_path, 'manifest.json')
            if not os.path.isdir(snapshot_path) or not os.path.isfile(manifest_path):
                continue
            try:
                with open(manifest_path, 'r', encoding='utf-8') as handle:
                    manifest = json.load(handle)
                    files = manifest.get('files') or []
            except (OSError, ValueError, TypeError):
                continue
            allowed = {'system.db', *(f'{user}.db' for user in USERS)}
            names = [entry.get('name') for entry in files if isinstance(entry, dict)]
            attachments = []
            try:
                with open(manifest_path, 'r', encoding='utf-8') as handle:
                    attachments = json.load(handle).get('attachments') or []
            except (OSError, ValueError, TypeError):
                continue
            attachments_ok = all(
                isinstance(entry, dict)
                and str(entry.get('name', '')).startswith('uploads/customer_files/')
                and os.path.isfile(os.path.join(snapshot_path, entry['name']))
                for entry in attachments
            )
            if names and all(name in allowed and os.path.isfile(os.path.join(snapshot_path, name)) for name in names) and attachments_ok:
                backups.append({'date': f'{date_name}/{snapshot_name}', 'files': names,
                                'attachments': len(attachments), 'path': f'{date_name}/{snapshot_name}',
                                'created_at': manifest.get('created_at', ''),
                                'reason': manifest.get('reason', '')})
    return backups


def migrate_old_database():
    """将旧的 crm_reminders.db 迁移为用户 hamid.db"""
    old_path = os.path.join(DB_DIR, 'crm_reminders.db')
    hamid_path = get_user_db_path('hamid')
    if os.path.exists(old_path) and not os.path.exists(hamid_path):
        try:
            shutil.copy2(old_path, hamid_path)
            logger.info(f'已将旧数据库 {old_path} 迁移到 {hamid_path}')
            return True
        except Exception as e:
            logger.error(f'迁移旧数据库失败: {e}')
    return False


# ========== 用户表结构 ==========

# Keep the grade vocabulary small and explicit so the UI, imports and SQLite
# constraint all agree. The +/- variants are useful when a customer sits
# between two broad priority bands without introducing arbitrary labels.
CUSTOMER_LEVEL_VALUES = (
    'A', 'A+', 'A-',
    'B', 'B+', 'B-',
    'C', 'C+', 'C-',
    'D', 'D+', 'D-',
)

USER_TABLE_SQL = [
    # 客户表
    '''
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        company TEXT DEFAULT '',
        country TEXT DEFAULT '',
        level TEXT DEFAULT 'C' CHECK(level IN ('A', 'A+', 'A-', 'B', 'B+', 'B-', 'C', 'C+', 'C-', 'D', 'D+', 'D-')),
        type TEXT DEFAULT '' CHECK(type IN ('中间商', '终端', '')),
        website TEXT DEFAULT '',
        profile TEXT DEFAULT '',
        field TEXT DEFAULT '',
        status TEXT DEFAULT '未建联' CHECK(status IN ('未建联', '已建联', '跟进中', '成交', '流失')),
        notes TEXT DEFAULT '',
        system_notes TEXT DEFAULT '',
        last_contact TEXT DEFAULT '',
        next_follow_up TEXT DEFAULT '',
        manual_next_follow INTEGER DEFAULT 0,
        customer_type TEXT DEFAULT 'existing' CHECK(customer_type IN ('new', 'existing')),
        industry TEXT DEFAULT '',
        company_size TEXT DEFAULT '',
        annual_revenue TEXT DEFAULT '',
        tags TEXT DEFAULT '',
        import_source TEXT DEFAULT 'manual',
        external_source TEXT DEFAULT '',
        external_id TEXT DEFAULT '',
        attention_state TEXT DEFAULT '',
        attention_reason TEXT DEFAULT '',
        attention_updated_at TEXT DEFAULT '',
        attention_review_date TEXT DEFAULT '',
        is_pinned INTEGER DEFAULT 0,
        pinned_order INTEGER DEFAULT 0,
        pinned_at TEXT DEFAULT '',
        is_deleted INTEGER DEFAULT 0,
        deleted_at TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    ''',
    # 提醒表
    '''
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        title TEXT DEFAULT '',
        content TEXT DEFAULT '',
        reason TEXT DEFAULT '',
        remind_date TEXT NOT NULL,
        is_done INTEGER DEFAULT 0,
        reminder_type TEXT DEFAULT 'follow_up',
        completed_at TEXT DEFAULT '',
        source_activity_id INTEGER,
        manual_order INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
    )
    ''',
    # 跟进历史
    '''
    CREATE TABLE IF NOT EXISTS follow_up_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        content TEXT DEFAULT '',
        follow_date TEXT NOT NULL,
        result TEXT DEFAULT '',
        next_plan TEXT DEFAULT '',
        activity_type TEXT DEFAULT 'follow_up',
        direction TEXT DEFAULT 'unknown',
        contact_id INTEGER,
        related_task_id INTEGER,
        source TEXT DEFAULT 'manual',
        is_reported INTEGER DEFAULT 0,
        is_deleted INTEGER DEFAULT 0,
        deleted_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
    )
    ''',
    # Browser communication capture keeps raw source material separate from
    # the editable activity fields and makes each imported message idempotent.
    '''
    CREATE TABLE IF NOT EXISTS communication_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        activity_id INTEGER NOT NULL UNIQUE,
        channel TEXT NOT NULL,
        source_url TEXT DEFAULT '',
        account TEXT DEFAULT '',
        conversation_identity TEXT DEFAULT '',
        adapter_version TEXT DEFAULT '',
        extraction_scope TEXT DEFAULT '',
        warnings TEXT DEFAULT '[]',
        raw_payload TEXT NOT NULL,
        cleaned_payload TEXT DEFAULT '',
        captured_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (activity_id) REFERENCES follow_up_logs(id) ON DELETE CASCADE
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS communication_source_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_fingerprint TEXT NOT NULL UNIQUE,
        activity_id INTEGER NOT NULL,
        message_time TEXT DEFAULT '',
        direction TEXT DEFAULT 'unknown',
        raw_text TEXT DEFAULT '',
        FOREIGN KEY (activity_id) REFERENCES follow_up_logs(id) ON DELETE CASCADE
    )
    ''',
    # 联系人
    '''
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        name TEXT DEFAULT '',
        title TEXT DEFAULT '',
        email TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        whatsapp TEXT DEFAULT '',
        linkedin TEXT DEFAULT '',
        preferred_channel TEXT DEFAULT '',
        contact_type TEXT DEFAULT 'person',
        is_primary INTEGER DEFAULT 0,
        notes TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
    )
    ''',
    # 开发信
    '''
    CREATE TABLE IF NOT EXISTS outreach_emails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        subject TEXT DEFAULT '',
        content TEXT DEFAULT '',
        sent_date TEXT DEFAULT '',
        reply_status TEXT DEFAULT 'pending' CHECK(reply_status IN ('pending', 'replied', 'bounced', 'no_reply')),
        reply_content TEXT DEFAULT '',
        reply_date TEXT DEFAULT '',
        is_reported INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        external_source TEXT DEFAULT '',
        external_id TEXT DEFAULT '',
        external_updated_at TEXT DEFAULT '',
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
    )
    ''',
    # Idempotency receipts for remote integrations. A client may retry after a
    # network timeout, so the server must be able to return the original
    # result without replaying the business mutation.
    '''
    CREATE TABLE IF NOT EXISTS integration_sync_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        integration TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        candidate_id TEXT DEFAULT '',
        customer_id INTEGER,
        response_json TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime')),
        UNIQUE(integration, idempotency_key),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL
    )
    ''',
    # 背调报告
    '''
    CREATE TABLE IF NOT EXISTS research_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL UNIQUE,
        summary TEXT DEFAULT '',
        company_info TEXT DEFAULT '',
        key_findings TEXT DEFAULT '',
        needs_analysis TEXT DEFAULT '',
        cooperation_value TEXT DEFAULT '',
        raw_input TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
    )
    ''',
    # 用户从外部模型带回、主动保存的分析结果；与系统生成报告分开保存。
    '''
    CREATE TABLE IF NOT EXISTS external_analysis_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        content TEXT NOT NULL,
        source TEXT DEFAULT 'external_model',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
    )
    ''',
    # AI keeps a concise, auditable working understanding instead of repeatedly
    # regenerating a long customer report from scratch.
    '''
    CREATE TABLE IF NOT EXISTS customer_understandings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL UNIQUE,
        current_summary TEXT DEFAULT '',
        recent_change TEXT DEFAULT '',
        open_loops TEXT DEFAULT '[]',
        action_state TEXT DEFAULT 'hold' CHECK(action_state IN ('act', 'wait', 'hold')),
        action_reason TEXT DEFAULT '',
        source_activity_id INTEGER,
        version INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE,
        FOREIGN KEY (source_activity_id) REFERENCES follow_up_logs(id) ON DELETE SET NULL
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS ai_recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        understanding_version INTEGER DEFAULT 0,
        content TEXT NOT NULL,
        reason TEXT DEFAULT '',
        source_activity_id INTEGER,
        review_status TEXT DEFAULT 'hold' CHECK(review_status IN ('display', 'rewrite', 'hold')),
        user_response TEXT DEFAULT '',
        user_modified_content TEXT DEFAULT '',
        executed_action TEXT DEFAULT '',
        outcome TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE,
        FOREIGN KEY (source_activity_id) REFERENCES follow_up_logs(id) ON DELETE SET NULL
    )
    ''',
    # 操作日志
    '''
    CREATE TABLE IF NOT EXISTS operation_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id INTEGER,
        details TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    ''',
    # Agent-originated writes remain proposals until the user explicitly confirms them.
    '''
    CREATE TABLE IF NOT EXISTS agent_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proposal_type TEXT NOT NULL CHECK(proposal_type IN ('task', 'activity')),
        customer_id INTEGER NOT NULL,
        payload TEXT NOT NULL,
        proposal_action TEXT DEFAULT '',
        source TEXT DEFAULT '',
        source_reference TEXT DEFAULT '',
        idempotency_key TEXT DEFAULT '',
        request_sha256 TEXT DEFAULT '',
        status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'confirmed', 'cancelled')),
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        confirmed_at TEXT DEFAULT '',
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
    )
    ''',
    # Gateway proposal retries are handled per user database.  The original
    # request hash detects reuse of a key for different business content.
    '''
    CREATE TABLE IF NOT EXISTS agent_gateway_idempotency (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        proposal_id INTEGER,
        response_json TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime')),
        UNIQUE(action, idempotency_key),
        FOREIGN KEY (proposal_id) REFERENCES agent_proposals(id) ON DELETE SET NULL
    )
    ''',
    # Direct Agent writes point at the same durable undo snapshot as the CRM
    # action.  This adds attribution and a stable action id without duplicating
    # the business data or its rollback representation.
    '''
    CREATE TABLE IF NOT EXISTS agent_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action_id TEXT NOT NULL UNIQUE,
        token_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        action_type TEXT NOT NULL,
        customer_id INTEGER,
        related_type TEXT DEFAULT '',
        related_id INTEGER,
        undo_token TEXT NOT NULL,
        request_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'completed' CHECK(status IN ('completed', 'undone')),
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        undone_at TEXT DEFAULT '',
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL
    )
    ''',
    # Every user-confirmed mutation stores a before/after snapshot. Undo is
    # conflict-aware: a later edit prevents an old snapshot from overwriting it.
    '''
    CREATE TABLE IF NOT EXISTS undo_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL UNIQUE,
        operation TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id INTEGER,
        description TEXT DEFAULT '',
        entities TEXT NOT NULL,
        status TEXT DEFAULT 'available' CHECK(status IN ('available', 'undone', 'blocked')),
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        undone_at TEXT DEFAULT ''
    )
    ''',
    # Excel recovery is auditable and idempotent: each original cell maps to one activity hash.
    '''
    CREATE TABLE IF NOT EXISTS import_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_name TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        imported_at TEXT DEFAULT (datetime('now', 'localtime')),
        imported_count INTEGER DEFAULT 0,
        skipped_count INTEGER DEFAULT 0,
        created_customers INTEGER DEFAULT 0,
        details TEXT DEFAULT ''
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS imported_activity_rows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        activity_hash TEXT NOT NULL UNIQUE,
        source_key TEXT DEFAULT '',
        batch_id INTEGER,
        customer_id INTEGER NOT NULL,
        source_name TEXT NOT NULL,
        source_sheet TEXT DEFAULT '',
        source_cell TEXT DEFAULT '',
        source_header TEXT DEFAULT '',
        activity_id INTEGER,
        imported_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE,
        FOREIGN KEY (batch_id) REFERENCES import_batches(id) ON DELETE SET NULL
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS import_unmatched_customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        unmatched_hash TEXT DEFAULT '',
        batch_id INTEGER,
        customer_name TEXT NOT NULL,
        country TEXT DEFAULT '',
        website TEXT DEFAULT '',
        source_sheet TEXT DEFAULT '',
        source_row INTEGER,
        reason TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (batch_id) REFERENCES import_batches(id) ON DELETE CASCADE
    )
    ''',
    # Inbox is deliberately small: it stores only items that still need a human decision.
    '''
    CREATE TABLE IF NOT EXISTS inbox_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_type TEXT NOT NULL,
        customer_id INTEGER,
        title TEXT NOT NULL,
        content TEXT DEFAULT '',
        dedupe_key TEXT DEFAULT '' UNIQUE,
        status TEXT DEFAULT 'open' CHECK(status IN ('open', 'archived', 'resolved')),
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        resolved_at TEXT DEFAULT '',
        snoozed_until TEXT DEFAULT '',
        resolution_reason TEXT DEFAULT '',
        resolution_note TEXT DEFAULT '',
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
    )
    ''',
    # 邮件发送记录（保留但不再前台展示）
    '''
    CREATE TABLE IF NOT EXISTS email_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL,
        message TEXT DEFAULT '',
        reminder_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    ''',
    # 邮箱可发送性检查结果。每个用户维护自己的本地验证缓存。
    '''
    CREATE TABLE IF NOT EXISTS email_verifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        normalized_email TEXT DEFAULT '',
        domain TEXT DEFAULT '',
        deliverability_status TEXT DEFAULT 'unknown',
        confidence TEXT DEFAULT 'low',
        address_type TEXT DEFAULT 'person',
        risk_flags TEXT DEFAULT '[]',
        evidence TEXT DEFAULT '[]',
        mx_records TEXT DEFAULT '[]',
        checked_at TEXT DEFAULT (datetime('now', 'localtime')),
        expires_at TEXT DEFAULT ''
    )
    ''',
    # 发送、退信和回复提供的邮箱级证据；为后续邮件接入保留。
    '''
    CREATE TABLE IF NOT EXISTS email_delivery_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        contact_id INTEGER,
        outreach_email_id INTEGER,
        event_type TEXT NOT NULL,
        smtp_code TEXT DEFAULT '',
        enhanced_status TEXT DEFAULT '',
        diagnostic_text TEXT DEFAULT '',
        remote_mta TEXT DEFAULT '',
        message_id TEXT DEFAULT '',
        source TEXT DEFAULT 'manual',
        occurred_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL,
        FOREIGN KEY (outreach_email_id) REFERENCES outreach_emails(id) ON DELETE SET NULL
    )
    ''',
    # SMTP 探测在独立调度任务中执行，避免导入接口被网络超时阻塞。
    '''
    CREATE TABLE IF NOT EXISTS email_verification_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        domain TEXT DEFAULT '',
        status TEXT DEFAULT 'queued' CHECK(status IN ('queued', 'running', 'completed', 'failed')),
        attempts INTEGER DEFAULT 0,
        next_run_at TEXT DEFAULT (datetime('now', 'localtime')),
        last_error TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    ''',
    # Catch-all 判断按域名缓存，控制对同一邮件域名的探测频率。
    '''
    CREATE TABLE IF NOT EXISTS email_domain_probes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain TEXT NOT NULL UNIQUE,
        catchall_status TEXT DEFAULT 'unknown',
        evidence TEXT DEFAULT '[]',
        checked_at TEXT DEFAULT (datetime('now', 'localtime')),
        next_check_at TEXT DEFAULT ''
    )
    ''',
    # 官网监控日志
    '''
    CREATE TABLE IF NOT EXISTS web_monitor_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        url TEXT DEFAULT '',
        status TEXT DEFAULT 'ok' CHECK(status IN ('ok', 'error', 'changed')),
        content_hash TEXT DEFAULT '',
        content_snippet TEXT DEFAULT '',
        change_summary TEXT DEFAULT '',
        checked_at TEXT DEFAULT (datetime('now', 'localtime')),
        reminder_id INTEGER,
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
    )
    ''',
    # 客户文件：挂在客户资料下的附件，元数据入库、二进制文件落盘
    # （uploads/customer_files/）。数据库保留来源、校验与归属信息，
    # 文件本体由备份清单单独校验并恢复。
    '''
    CREATE TABLE IF NOT EXISTS customer_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        original_name TEXT NOT NULL,
        stored_name TEXT NOT NULL,
        file_path TEXT NOT NULL,
        file_size INTEGER DEFAULT 0,
        mime_type TEXT DEFAULT '',
        category TEXT DEFAULT '',
        sha256 TEXT DEFAULT '',
        uploaded_by TEXT DEFAULT '',
        is_deleted INTEGER DEFAULT 0,
        deleted_at TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
    )
    ''',
]

# 用户数据库迁移：为旧库加新列
USER_MIGRATIONS = {
    'customers': {
        # Older customer databases predate the audit timestamp. Keep this
        # migration explicit because customer list sorting relies on it.
        'updated_at': "TEXT DEFAULT ''",
        'system_notes': "TEXT DEFAULT ''",
        'manual_next_follow': "INTEGER DEFAULT 0",
        'last_contact': "TEXT DEFAULT ''",
        'customer_type': "TEXT DEFAULT 'existing'",
        'industry': "TEXT DEFAULT ''",
        'company_size': "TEXT DEFAULT ''",
        'annual_revenue': "TEXT DEFAULT ''",
        'tags': "TEXT DEFAULT ''",
        'import_source': "TEXT DEFAULT 'manual'",
        'external_source': "TEXT DEFAULT ''",
        'external_id': "TEXT DEFAULT ''",
        'attention_state': "TEXT DEFAULT ''",
        'attention_reason': "TEXT DEFAULT ''",
        'attention_updated_at': "TEXT DEFAULT ''",
        'attention_review_date': "TEXT DEFAULT ''",
        'is_pinned': "INTEGER DEFAULT 0",
        'pinned_order': "INTEGER DEFAULT 0",
        'pinned_at': "TEXT DEFAULT ''",
        'is_deleted': "INTEGER DEFAULT 0",
        'deleted_at': "TEXT DEFAULT ''",
    },
    'reminders': {
        'reminder_type': "TEXT DEFAULT 'follow_up'",
        'title': "TEXT DEFAULT ''",
        'reason': "TEXT DEFAULT ''",
        'completed_at': "TEXT DEFAULT ''",
        'source_activity_id': "INTEGER",
        'manual_order': "INTEGER DEFAULT 0",
        # Task editing records an audit timestamp. Keep legacy reminder
        # databases compatible with the current update queries.
        'updated_at': "TEXT DEFAULT ''",
    },
    'follow_up_logs': {
        'source': "TEXT DEFAULT 'manual'",
        'is_reported': "INTEGER DEFAULT 0",
        'activity_type': "TEXT DEFAULT 'follow_up'",
        'direction': "TEXT DEFAULT 'unknown'",
        'contact_id': "INTEGER",
        'related_task_id': "INTEGER",
        'is_deleted': "INTEGER DEFAULT 0",
        'deleted_at': "TEXT DEFAULT ''",
        'updated_at': "TEXT DEFAULT ''",
    },
    'outreach_emails': {
        'is_reported': "INTEGER DEFAULT 0",
        'recipient_email': "TEXT DEFAULT ''",
        'contact_id': "INTEGER",
        'message_id': "TEXT DEFAULT ''",
        'external_source': "TEXT DEFAULT ''",
        'external_id': "TEXT DEFAULT ''",
        'external_updated_at': "TEXT DEFAULT ''",
    },
    'contacts': {
        'whatsapp': "TEXT DEFAULT ''",
        'preferred_channel': "TEXT DEFAULT ''",
        'contact_type': "TEXT DEFAULT 'person'",
    },
    'inbox_items': {
        'snoozed_until': "TEXT DEFAULT ''",
        'resolution_reason': "TEXT DEFAULT ''",
        'resolution_note': "TEXT DEFAULT ''",
    },
    'research_reports': {
        'source': "TEXT DEFAULT 'manual'",
        'web_content': "TEXT DEFAULT ''",
        'web_fetched_at': "TEXT DEFAULT ''",
        'expires_at': "TEXT DEFAULT ''",
    },
    'import_unmatched_customers': {
        'unmatched_hash': "TEXT DEFAULT ''",
    },
    'imported_activity_rows': {
        'source_key': "TEXT DEFAULT ''",
    },
    'agent_proposals': {
        'proposal_action': "TEXT DEFAULT ''",
        'source': "TEXT DEFAULT ''",
        'source_reference': "TEXT DEFAULT ''",
        'idempotency_key': "TEXT DEFAULT ''",
        'request_sha256': "TEXT DEFAULT ''",
    },
    'agent_actions': {
        'user_id': "TEXT DEFAULT ''",
    },
}


def _merge_duplicate_contact_emails(cursor):
    """Merge legacy contacts that share one normalized email inside a customer."""
    cursor.execute('''SELECT customer_id, lower(trim(email)) AS normalized_email
                      FROM contacts WHERE trim(email) <> ''
                      GROUP BY customer_id, lower(trim(email)) HAVING COUNT(*) > 1''')
    groups = cursor.fetchall()
    merged_count = 0
    generic_names = {'公司公共邮箱', '公共邮箱', '联系人', 'contact', 'info'}
    merge_fields = ('name', 'title', 'phone', 'whatsapp', 'linkedin',
                    'preferred_channel', 'contact_type', 'notes')
    for customer_id, normalized_email in groups:
        cursor.execute('''SELECT * FROM contacts
                          WHERE customer_id=? AND lower(trim(email))=?
                          ORDER BY is_primary DESC, id ASC''', (customer_id, normalized_email))
        rows = [dict(row) for row in cursor.fetchall()]
        if len(rows) < 2:
            continue
        survivor = rows[0]
        for duplicate in rows[1:]:
            for field in merge_fields:
                if not survivor.get(field) and duplicate.get(field):
                    survivor[field] = duplicate[field]
            old_name = (survivor.get('name') or '').strip().casefold()
            new_name = (duplicate.get('name') or '').strip()
            if new_name and old_name in generic_names and new_name.casefold() not in generic_names:
                survivor['name'] = new_name
            survivor['is_primary'] = max(survivor.get('is_primary') or 0, duplicate.get('is_primary') or 0)
            cursor.execute('DELETE FROM contacts WHERE id=?', (duplicate['id'],))
            merged_count += 1
        cursor.execute('''UPDATE contacts SET name=?, title=?, email=?, phone=?, whatsapp=?, linkedin=?,
                          preferred_channel=?, contact_type=?, is_primary=?, notes=? WHERE id=?''',
                       (survivor.get('name') or '', survivor.get('title') or '', normalized_email,
                        survivor.get('phone') or '', survivor.get('whatsapp') or '', survivor.get('linkedin') or '',
                        survivor.get('preferred_channel') or '', survivor.get('contact_type') or 'person',
                        survivor.get('is_primary') or 0, survivor.get('notes') or '', survivor['id']))
    cursor.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_customer_email_unique
                      ON contacts(customer_id, lower(trim(email))) WHERE trim(email) <> '' ''')
    return merged_count


def _migrate_customer_level_constraint(cursor):
    """Allow +/- customer grades while preserving the existing customer rows.

    SQLite cannot alter a CHECK constraint in place. Rebuild only the
    customers table, keep all existing columns and indexes, and normalize any
    legacy unexpected value to C while copying.
    """
    table_row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='customers'"
    ).fetchone()
    table_sql = (table_row[0] or '') if table_row else ''
    if "'A-'" in table_sql:
        return

    connection = cursor.connection
    connection.commit()
    foreign_keys_enabled = bool(cursor.execute('PRAGMA foreign_keys').fetchone()[0])
    index_rows = cursor.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name='customers' AND sql IS NOT NULL"
    ).fetchall()
    old_columns = [row[1] for row in cursor.execute('PRAGMA table_info(customers)').fetchall()]

    cursor.execute('PRAGMA foreign_keys=OFF')
    try:
        cursor.execute('DROP TABLE IF EXISTS customers_new')
        create_sql = USER_TABLE_SQL[0].replace(
            'CREATE TABLE IF NOT EXISTS customers',
            'CREATE TABLE customers_new',
            1,
        )
        cursor.execute(create_sql)
        new_columns = [row[1] for row in cursor.execute('PRAGMA table_info(customers_new)').fetchall()]
        columns = [name for name in old_columns if name in new_columns]
        quoted_columns = ', '.join('"' + name.replace('"', '""') + '"' for name in columns)
        allowed_sql = ', '.join("'" + value.replace("'", "''") + "'" for value in CUSTOMER_LEVEL_VALUES)
        select_columns = [
            "CASE WHEN \"level\" IN (" + allowed_sql + ") THEN \"level\" ELSE 'C' END"
            if name == 'level' else '"' + name.replace('"', '""') + '"'
            for name in columns
        ]
        cursor.execute(
            'INSERT INTO customers_new (' + quoted_columns + ') SELECT ' +
            ', '.join(select_columns) + ' FROM customers'
        )
        for index_name, _index_sql in index_rows:
            cursor.execute('DROP INDEX "' + index_name.replace('"', '""') + '"')
        cursor.execute('DROP TABLE customers')
        cursor.execute('ALTER TABLE customers_new RENAME TO customers')
        for _index_name, index_sql in index_rows:
            cursor.execute(index_sql)
    finally:
        cursor.execute('PRAGMA foreign_keys=' + ('1' if foreign_keys_enabled else '0'))


def init_user_tables(user):
    """初始化/迁移单个用户的数据库"""
    old_user = get_current_user()
    set_db_user(user)
    try:
        conn = get_db()
        c = conn.cursor()
        
        # 创建所有表
        for sql in USER_TABLE_SQL:
            c.execute(sql)
        
        # 数据库迁移
        for table_name, migrations in USER_MIGRATIONS.items():
            try:
                c.execute(f"PRAGMA table_info({table_name})")
                existing_cols = [row[1] for row in c.fetchall()]
                for col_name, col_def in migrations.items():
                    if col_name not in existing_cols:
                        c.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")
            except Exception as e:
                logger.debug(f"迁移 {table_name} 跳过: {e}")

        _migrate_customer_level_constraint(c)

        merged_contacts = _merge_duplicate_contact_emails(c)
        if merged_contacts:
            logger.info(f'{user}: 已合并 {merged_contacts} 条重复邮箱联系人')

        # 旧提醒继续可用；新代码优先读取 title，旧数据用 content 回填。
        c.execute("UPDATE reminders SET title = content WHERE title IS NULL OR title = ''")
        c.execute("SELECT id, customer_id, source_sheet, source_header FROM imported_activity_rows WHERE COALESCE(source_key, '') = ''")
        for imported_row in c.fetchall():
            source_key = hashlib.sha256(
                f'{imported_row[1]}|{(imported_row[2] or "").strip().casefold()}|{(imported_row[3] or "").strip().casefold()}'.encode('utf-8')
            ).hexdigest()
            c.execute('UPDATE imported_activity_rows SET source_key=? WHERE id=?', (source_key, imported_row[0]))
        c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_import_unmatched_hash
                     ON import_unmatched_customers(unmatched_hash)
                     WHERE unmatched_hash != '' ''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_email_delivery_events_email_time
                     ON email_delivery_events(lower(trim(email)), occurred_at DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_email_verification_jobs_next_run
                     ON email_verification_jobs(status, next_run_at)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_ai_recommendations_customer_time
                     ON ai_recommendations(customer_id, created_at DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_external_analysis_notes_customer_time
                     ON external_analysis_notes(customer_id, created_at DESC)''')
        # Match the high-frequency dashboard, Inbox and customer-list queries.
        c.execute('''CREATE INDEX IF NOT EXISTS idx_reminders_open_date
                     ON reminders(is_done, remind_date, customer_id)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_reminders_customer_open_date
                     ON reminders(customer_id, is_done, remind_date)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_contacts_customer_primary
                     ON contacts(customer_id, is_primary DESC, created_at DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_contacts_email_lookup
                     ON contacts(lower(trim(email))) WHERE email IS NOT NULL AND trim(email) != '' ''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_follow_logs_customer_date
                     ON follow_up_logs(customer_id, follow_date DESC, created_at DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_follow_logs_reported_date
                     ON follow_up_logs(is_reported, follow_date, is_deleted, customer_id)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_outreach_customer_date
                     ON outreach_emails(customer_id, sent_date DESC, created_at DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_outreach_reported_date
                     ON outreach_emails(is_reported, sent_date, customer_id)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_inbox_status_type_customer
                     ON inbox_items(status, item_type, customer_id, created_at DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_web_monitor_customer_status_date
                     ON web_monitor_logs(customer_id, status, checked_at DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_agent_proposals_pending
                     ON agent_proposals(status, customer_id, created_at DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_agent_gateway_idempotency_key
                     ON agent_gateway_idempotency(action, idempotency_key)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_agent_actions_created
                     ON agent_actions(created_at DESC, action_id)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_undo_actions_status_time
                     ON undo_actions(status, created_at DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_customers_pinned_order
                     ON customers(is_deleted, is_pinned DESC, pinned_order ASC, updated_at DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_customers_active_updated
                     ON customers(is_deleted, updated_at DESC)''')
        c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_external_identity
                     ON customers(external_source, external_id)
                     WHERE trim(external_source) <> '' AND trim(external_id) <> '' ''')
        c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_outreach_external_identity
                     ON outreach_emails(external_source, external_id)
                     WHERE trim(external_source) <> '' AND trim(external_id) <> '' ''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_integration_sync_receipts_key
                     ON integration_sync_receipts(integration, idempotency_key)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_customer_files_customer
                     ON customer_files(customer_id, is_deleted, created_at DESC)''')
        
        conn.commit()
        conn.close()
    finally:
        set_db_user(old_user)
    
    # 插入演示数据（仅当数据库为空时）
    seed_demo_data_for_user(user)


def seed_demo_data_for_user(user):
    """为指定用户插入演示数据（仅 Hamid 有演示数据，Amy/Kelley 为空）"""
    if os.environ.get('CRM_SEED_DEMO_DATA', '').strip().lower() not in ('1', 'true', 'yes'):
        return
    if user not in ('hamid',):
        return
    old_user = get_current_user()
    set_db_user(user)
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM customers")
        if c.fetchone()[0] > 0:
            conn.close()
            set_db_user(old_user)
            return
        
        today = datetime.now().strftime('%Y-%m-%d')
        tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        next_week = (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d')
        
        demo_customers = [
            ('ProSEP S.R.L.', 'ProSEP S.R.L.', '罗马尼亚', 'C+', '中间商', 'https://prosep.ro/',
             '罗马尼亚主要的塑料半成品进口与分销商', '亚克力分销', '跟进中', '5/31领英添加好友→客户回复询问介绍资料', tomorrow),
            ('Mar Industrial', 'Mar Industrial Distribuidora', '墨西哥', 'C', '中间商', '',
             '墨西哥工程塑料分销，主营PC、亚克力、尼龙等', '工程塑料分销', '未建联', '寻求亚洲非中国产地的聚碳酸酯板材供应商', today),
            ('Regal Plastics', 'Regal Plastics', '美国', 'C+', '中间商', 'https://www.regal-plastics.com/',
             '美国大型塑料板材分销，主要供应商为泰国titan', '亚克力分销', '跟进中', '询价40尺整柜透明浇铸板，等待6月正式报价', next_week),
            ('Enseignes Valois', 'Enseignes Valois', '加拿大', 'C+', '终端', 'https://www.enseignesvalois.com/',
             '魁北克拉瓦勒的标识制造商，成立于2016年', '标牌制造', '跟进中', '已发送报价未回复', today),
            ('Bentleigh Group', 'Bentleigh Group', '澳大利亚', 'C+', '终端', '',
             '澳洲老牌标识企业，自营墨尔本、布里斯班两大工厂', '标牌制造', '跟进中', '样品已寄出，等待客户反馈', tomorrow),
        ]
        for cust in demo_customers:
            c.execute('''INSERT INTO customers (name, company, country, level, type, website, profile, field, status, notes, next_follow_up, created_at, updated_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))''', cust)
        
        c.execute('SELECT id, name, next_follow_up, notes FROM customers')
        for row in c.fetchall():
            c.execute('''
                INSERT INTO reminders (customer_id, title, content, reason, remind_date, is_done, reminder_type, created_at)
                VALUES (?, ?, ?, ?, ?, 0, 'follow_up', datetime('now'))
            ''', (row['id'], f'联系 {row["name"]}', f'联系 {row["name"]}', row['notes'], row['next_follow_up']))
        
        conn.commit()
        conn.close()
    finally:
        set_db_user(old_user)


# ========== 系统数据库初始化 ==========

def init_system_db():
    """初始化系统数据库（用户信息、周报等）"""
    conn = get_system_db()
    c = conn.cursor()
    
    # 用户表
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            label TEXT NOT NULL,
            color TEXT DEFAULT '#666',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    
    # 周报表
    c.execute('''
        CREATE TABLE IF NOT EXISTS weekly_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            week_start TEXT NOT NULL,
            content TEXT DEFAULT '',
            highlights TEXT DEFAULT '',
            challenges TEXT DEFAULT '',
            next_plan TEXT DEFAULT '',
            status TEXT DEFAULT 'draft' CHECK(status IN ('draft', 'submitted')),
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(user_id, week_start)
        )
    ''')
    
    # 应用设置
    c.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    
    # 插入默认用户
    for uid, info in USERS.items():
        c.execute('''
            INSERT OR IGNORE INTO users (id, name, label, color)
            VALUES (?, ?, ?, ?)
        ''', (uid, info['name'], info['label'], info['color']))
    
    conn.commit()
    conn.close()


# ========== 全部初始化 ==========

def init_all_dbs():
    """初始化所有数据库"""
    ensure_db_dir()
    ensure_db_identity()
    
    # 1. 迁移旧数据库
    migrate_old_database()
    
    # 2. 初始化系统数据库
    init_system_db()
    
    # 3. 初始化每个用户的数据库
    for user in USERS:
        init_user_tables(user)
    
    # 4. 执行一次完整性检查
    integrity = check_integrity()
    for name, status in integrity.items():
        if status != 'ok':
            logger.warning(f'数据库完整性检查 [{name}]: {status}')
    
    logger.info('所有数据库初始化完成')
