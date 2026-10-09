#!/usr/bin/env python3
"""Run one Trosa tree as an isolated loopback dev server for browser reproductions.

``app.py``'s own ``__main__`` block binds ``0.0.0.0``, which would expose a
scratch database to the office LAN.  This runner performs the same startup work
(database initialisation and housekeeping) and then binds ``127.0.0.1`` only.
It always uses SQLite in development mode, clears any PostgreSQL setting from
the caller's environment, and refuses a database directory inside a repository
``data/`` folder.

Run it from the repository root, or through the desktop app's preview
configuration in ``.claude/launch.json`` (see docs/REAL_BROWSER_REPRO.md):

    .venv/bin/python tools/repro_dev_server.py --app-root . --port 18190 --db-dir /path/to/scratch/db
"""
import argparse
import importlib.util
import os
import sys
from pathlib import Path

# Office-LAN entry points (the weekly board shown on the sign-in screen) exist
# only for internal viewers.  Loopback is treated as internal so the browser can
# reproduce that entry; the database itself stays a throwaway local copy.
DEFAULT_INTERNAL_VIEWER_CIDRS = '127.0.0.1/32,::1/128'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Isolated loopback dev server for browser reproductions.')
    parser.add_argument('--app-root', required=True,
                        help='directory that holds app.py and db.py (the working tree or a clean copy)')
    parser.add_argument('--port', type=int, required=True, help='loopback port to listen on')
    parser.add_argument('--db-dir', required=True,
                        help='scratch directory for SQLite files; must not be a repository data/ folder')
    return parser.parse_args(argv)


def check_paths(app_root, db_dir):
    if not (app_root / 'app.py').is_file() or not (app_root / 'db.py').is_file():
        raise SystemExit(f'not a Trosa tree (app.py/db.py missing): {app_root}')
    repo_data_dirs = {app_root / 'data', Path(__file__).resolve().parents[1] / 'data'}
    for data_dir in repo_data_dirs:
        if db_dir == data_dir or data_dir in db_dir.parents:
            raise SystemExit(f'refusing to use a repository data folder for repro data: {db_dir}')


def main(argv=None):
    args = parse_args(argv)
    app_root = Path(args.app_root).resolve()
    db_dir = Path(args.db_dir).expanduser().resolve()
    check_paths(app_root, db_dir)
    if not 1024 <= args.port <= 65535:
        raise SystemExit('port must be between 1024 and 65535')

    # Set the runtime contract before importing any Trosa module: db.py reads
    # these at import time.  Production and PostgreSQL settings are overridden on
    # purpose so a stray shell variable can never point this server at a real
    # store.
    os.environ.update({
        'CRM_ENV': 'development',
        'TRADE_OS_DEV_SQLITE': '1',
        'TRADE_OS_DATA_BACKEND': 'sqlite',
        'CRM_DB_PATH': str(db_dir),
        'CRM_PORT': str(args.port),
    })
    os.environ.pop('TRADE_OS_DATABASE_URL', None)
    os.environ.setdefault('CRM_INTERNAL_VIEWER_CIDRS', DEFAULT_INTERNAL_VIEWER_CIDRS)

    sys.path.insert(0, str(app_root))
    spec = importlib.util.spec_from_file_location('trosa_repro_app', app_root / 'app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    import db
    db.init_all_dbs()
    db.run_startup_maintenance()
    print(f'[repro] app={app_root} db={db.DB_DIR} url=http://127.0.0.1:{args.port}', flush=True)
    module.app.run(host='127.0.0.1', port=args.port, debug=False, threaded=True, use_reloader=False)


if __name__ == '__main__':
    main()
