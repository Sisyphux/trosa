"""Production entrypoint for Trade OS.

Run this file through Waitress behind Cloudflare Tunnel.  The normal public
hostname still reaches the loopback listener through the Tunnel.  When the
production environment explicitly sets ``CRM_BIND_HOST=0.0.0.0``, the same
listener is also reachable on the office LAN; the Flask app grants that path
only the configured read-only internal viewer permissions.
"""
import atexit
import importlib.util
import logging
import os
import signal
from pathlib import Path

from waitress import serve

from db import init_all_dbs, run_startup_maintenance
from scheduler import start_scheduler, stop_scheduler


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _load_flask_app():
    """Load the root app.py without colliding with the app/ package."""
    entrypoint = Path(__file__).with_name('app.py')
    spec = importlib.util.spec_from_file_location('trade_os_web', entrypoint)
    if not spec or not spec.loader:
        raise RuntimeError(f'无法加载应用入口：{entrypoint}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def shutdown(*_args):
    stop_scheduler()
    raise SystemExit(0)


def main():
    if os.environ.get('CRM_ENV', '').lower() != 'production':
        raise RuntimeError('生产服务请设置 CRM_ENV=production')
    init_all_dbs()
    run_startup_maintenance()
    start_scheduler()
    atexit.register(stop_scheduler)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    port = int(os.environ.get('CRM_PORT', '8080'))
    bind_host = os.environ.get('CRM_BIND_HOST', '127.0.0.1').strip() or '127.0.0.1'
    logger.info('Trade OS production server listening on %s:%s', bind_host, port)
    serve(_load_flask_app(), host=bind_host, port=port, threads=int(os.environ.get('CRM_THREADS', '8')))


if __name__ == '__main__':
    main()
