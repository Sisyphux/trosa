"""Non-production PostgreSQL entrypoint for the unified Trade OS rehearsal.

This entrypoint is intentionally separate from ``serve.py``.  It refuses to
start without the explicit PostgreSQL backend flag and binds to loopback by
default, so a rehearsal cannot silently become the production service.
"""

from __future__ import annotations

import atexit
import importlib.util
import logging
import os
import signal
from pathlib import Path

from waitress import serve

from db import init_all_dbs


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _load_flask_app():
    entrypoint = Path(__file__).with_name("app.py")
    spec = importlib.util.spec_from_file_location("trade_os_rehearsal_web", entrypoint)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载应用入口：{entrypoint}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def _shutdown(*_args):
    raise SystemExit(0)


def main():
    if os.environ.get("CRM_ENV", "rehearsal").lower() == "production":
        raise RuntimeError("演练入口不能以 CRM_ENV=production 启动")
    if os.environ.get("TRADE_OS_DATA_BACKEND", "").strip().lower() != "postgres":
        raise RuntimeError("演练入口必须设置 TRADE_OS_DATA_BACKEND=postgres")
    if not os.environ.get("TRADE_OS_DATABASE_URL", "").strip():
        raise RuntimeError("演练入口必须设置 TRADE_OS_DATABASE_URL")

    # Keep the existing filesystem fallback available for temporary uploads,
    # session markers and preview assets, but never use it as a business store.
    os.environ.setdefault("CRM_ENV", "rehearsal")
    os.environ.setdefault("CRM_DB_PATH", str(Path(__file__).with_name(".rehearsal-local")))
    init_all_dbs()
    atexit.register(lambda: None)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    app = _load_flask_app()
    host = os.environ.get("CRM_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("CRM_PORT", "18080"))
    threads = int(os.environ.get("CRM_THREADS", "8"))
    logger.info("Trade OS PostgreSQL rehearsal server listening on %s:%s", host, port)
    serve(app, host=host, port=port, threads=threads)


if __name__ == "__main__":
    main()
