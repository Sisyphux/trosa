"""Conservative local-storage maintenance for Trade OS.

Run directly for the same cleanup that the application performs at startup.
It only removes expired automatic snapshots, old non-authoritative uploads,
and the spreadsheet-audit temporary workspace. It never removes CRM databases,
source files, manually named backups, or authoritative upload sources.
"""
from db import run_startup_maintenance


if __name__ == '__main__':
    result = run_startup_maintenance()
    print(
        'Storage maintenance finished: removed {directories} directories, '
        '{files} files, freed {megabytes:.1f} MB.'.format(
            directories=result.get('removed_directories', 0),
            files=result.get('removed_files', 0),
            megabytes=result.get('removed_bytes', 0) / 1024 / 1024,
        )
    )
