# -*- coding: utf-8 -*-
"""
客户跟进提醒系统 - 桌面应用启动器
自己管理 Flask 启动 + 系统浏览器 --app 模式窗口。

支持两种运行方式:
  - 源码启动: python desktop.py
  - PyInstaller 打包: 双击 客户跟进提醒系统.exe
"""

import os
import sys
import time
import socket
import threading
import subprocess
import logging

# ============================================================
# 路径处理
# ============================================================
def _frozen_path(relative_path):
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


STATIC_FOLDER = _frozen_path('app/static')

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
    DATA_DIR = os.path.join(BASE_DIR, 'data')
    LOG_DIR = os.path.join(BASE_DIR, 'logs')
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.join(BASE_DIR, 'data')
    LOG_DIR = os.path.join(BASE_DIR, 'logs')

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    filename=os.path.join(LOG_DIR, 'desktop.log'),
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger(__name__)

PROJECT_DIR = (
    sys._MEIPASS if getattr(sys, 'frozen', False)
    else os.path.dirname(os.path.abspath(__file__))
)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# ============================================================
# 导入 Flask 应用
# ============================================================
try:
    # The project contains both app.py and an app/ package. On macOS/Python 3.12,
    # a normal ``import app`` can resolve to the package instead of the Flask
    # entry file. Load app.py explicitly so the desktop launcher is unambiguous.
    import importlib.util
    app_file = os.path.join(PROJECT_DIR, 'app.py')
    app_spec = importlib.util.spec_from_file_location('crm_flask_app', app_file)
    if app_spec is None or app_spec.loader is None:
        raise ImportError('无法加载 app.py')
    app_module = importlib.util.module_from_spec(app_spec)
    app_spec.loader.exec_module(app_module)
    app_module.app.static_folder = STATIC_FOLDER
    flask_app = app_module.app
    log.info("Flask app 导入成功, static_folder=%s", STATIC_FOLDER)
except Exception as e:
    log.exception("Flask app 导入失败")
    raise

HOST = '127.0.0.1'
PORT = 8080


def _wait_for_port(host, port, timeout=15):
    """等待端口可连接"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.3)
    return False


def _find_browser():
    """找到 Edge 或 Chrome 的路径"""
    candidates = [
        # Edge
        os.path.join(os.environ.get('PROGRAMFILES(X86)', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(os.environ.get('PROGRAMFILES', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        # Chrome
        os.path.join(os.environ.get('PROGRAMFILES', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(os.environ.get('PROGRAMFILES(X86)', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Google', 'Chrome', 'Application', 'chrome.exe'),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


def _production_service_alive():
    """Do not start a second source server when the launchd service owns 8080."""
    try:
        # A raw loopback probe avoids HTTP proxy settings and is sufficient
        # here: only the production Waitress service is allowed to own 8080.
        with socket.create_connection(('127.0.0.1', 8080), timeout=1.0):
            return True
    except (TimeoutError, OSError):
        return False


def main():
    log.info("=== 桌面应用启动 ===")
    log.info("PORT=%d, DATA_DIR=%s", PORT, DATA_DIR)

    # The production Waitress service is the single owner of port 8080.  A
    # desktop launcher is still useful as a browser opener, but it must not
    # create a second scheduler and Flask process against the same databases.
    if _production_service_alive():
        log.info("检测到生产 Trade OS 已运行，跳过源码服务启动")
        url = f'http://{HOST}:{PORT}'
        if sys.platform == 'darwin':
            subprocess.Popen(['open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import webbrowser
            webbrowser.open(url)
        return

    # Keep the macOS desktop path behavior identical to running app.py:
    # apply safe schema migrations before serving requests, then start jobs.
    try:
        app_module.init_all_dbs()
        app_module.run_startup_maintenance()
        log.info("数据库初始化与迁移完成")
    except Exception:
        log.exception("数据库初始化失败")
        raise

    try:
        app_module.start_scheduler()
        log.info("定时任务已启动")
    except Exception:
        # Scheduling is helpful but must not prevent users from opening the CRM.
        log.exception("定时任务启动失败，继续启动界面")

    # Source builds reload themselves after code/static updates, preventing a
    # stale Flask process from continuing to occupy port 8080.
    app_module.start_source_watchdog()

    # 1) 打开浏览器窗口（在启动 Flask 之前，这样浏览器会一直等重试连接）
    browser = _find_browser()
    url = f'http://{HOST}:{PORT}'

    if browser:
        log.info("使用浏览器: %s", browser)
        # 用独立 user-data-dir 避免和已打开的 Edge 冲突
        # 这样 --app 进程不会立即退出
        profile_dir = os.path.join(DATA_DIR, 'browser_profile')
        os.makedirs(profile_dir, exist_ok=True)

        browser_proc = subprocess.Popen([
            browser,
            '--app=' + url,
            '--window-size=1280,800',
            '--no-first-run',
            '--no-default-browser-check',
            '--user-data-dir=' + profile_dir,
        ])
        log.info("浏览器进程 PID=%d", browser_proc.pid)
    else:
        log.warning("未找到 Edge/Chrome，使用系统默认浏览器")
        if sys.platform == 'darwin':
            # macOS 的 webbrowser 在部分终端环境会错误解析 BROWSER，
            # 进而触发 AppleScript 语法错误。`open` 会可靠地交给系统处理。
            subprocess.Popen(
                ['open', url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            import webbrowser
            webbrowser.open(url)
        browser_proc = None

    # 2) 主线程直接运行 Flask（不是 daemon 线程）
    #    Flask 一直运行，浏览器窗口关闭后用户从任务栏关闭整个应用
    log.info("Flask 在主线程启动...")
    try:
        flask_app.run(host=HOST, port=PORT, debug=False, use_reloader=False)
    except Exception as e:
        log.exception("Flask 异常退出")
        raise
    finally:
        try:
            app_module.stop_scheduler()
        except Exception:
            log.exception("停止定时任务失败")


if __name__ == '__main__':
    main()
