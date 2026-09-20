#!/usr/bin/env python3
"""Safer rehearsal-process hygiene: monitor and reclaim leftovers from gate runs.

Why this exists
---------------
The release gate (``deploy/cloud/release-test.sh``) starts isolated rehearsal
services: a ``serve_rehearsal.py`` web server (one per browser acceptance), a
loopback PostgreSQL rehearsal cluster, and a pile of temporary directories under
``$TMPDIR``.  When a gate is interrupted, ``SIGKILL``-ed, or a child outlives
the shell that launched it, those resources are never reclaimed.  Over a day of
parallel agent work this accumulates into dozens of orphaned web servers, each
holding an ephemeral port and burning CPU, which makes every later gate appear
to "hang" because it is competing for the machine.

This tool is the single, explicit program that monitors and cleans that
accumulation.  It is deliberately conservative:

* A rehearsal web process is only ever reclaimed when its parent is gone
  (reparented to PID 1).  A process with a live parent is part of an in-flight
  gate and is reported as ``active`` -- never touched.
* PostgreSQL rehearsal clusters are only stopped with ``--include-postgres``
  and only when no live gate references their tree/port.
* Temporary artifacts are only removed when no live process references them and
  they are older than ``--min-temp-age``.
* ``clean`` is a dry run unless ``--apply`` is given.

Commands
--------
::

    python3 tools/rehearsal_hygiene.py scan            # report (default)
    python3 tools/rehearsal_hygiene.py scan --json
    python3 tools/rehearsal_hygiene.py clean           # dry run
    python3 tools/rehearsal_hygiene.py clean --apply   # actually reclaim
    python3 tools/rehearsal_hygiene.py watch --interval 60 --apply

Exit codes: ``0`` success, ``2`` ``scan --check`` found reclaimable orphans,
``1`` usage/runtime error.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

REHEARSAL_WEB_MARKER = "serve_rehearsal.py"
POSTGRES_REHEARSAL_MARKER = "postgres-rehearsal"
GATE_MARKERS = ("release-test.sh", "browser_acceptance.sh", "agent-worktree.sh")

# Prefixes of temporary artifacts created by the gate/rehearsal tooling.
TEMP_PREFIXES: dict[str, str] = {
    "trosa-release.": "release worktree",
    "trosa-release-driver.": "release driver worktree",
    "trosa-release-gate.": "gate log dir",
    "trosa-release-test.": "python regression data dir",
    "trosa-release-test-env.": "gate env file",
    "trosa-release-net-": "release network temp",
    "trosa-pg-": "rehearsal pg socket dir",
    "trosa-browser-acceptance.": "browser acceptance log",
}
WORKTREE_PREFIXES = ("trosa-release.", "trosa-release-driver.")

DEFAULT_MIN_TEMP_AGE = 3600        # seconds before an unreferenced temp artifact is stale
DEFAULT_MIN_PROCESS_AGE = 10       # ignore brand-new orphans to avoid fork races
TERM_GRACE_SECONDS = 5.0


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    etime_seconds: int
    command: str

    @property
    def tokens(self) -> list[str]:
        return self.command.split()

    def argv(self) -> list[str]:
        try:
            return shlex.split(self.command)
        except ValueError:
            return self.command.split()


@dataclasses.dataclass
class WebFinding:
    pid: int
    ppid: int
    tree: str
    age_seconds: int
    port: int | None
    status: str  # active | orphan


@dataclasses.dataclass
class PostgresFinding:
    pid: int
    tree: str
    data_dir: str
    port: int | None
    age_seconds: int
    status: str  # in-use | idle


@dataclasses.dataclass
class TempFinding:
    path: str
    kind: str       # dir | file | worktree
    purpose: str
    age_seconds: int | None
    referenced_by: list[int]
    status: str     # referenced | stale | recent


# --------------------------------------------------------------------------- #
# Pure parsing / classification helpers (unit-tested without real processes)
# --------------------------------------------------------------------------- #

def parse_etime(value: str) -> int:
    """Parse ``ps`` etime (``[[DD-]HH:]MM:SS``) into seconds; 0 if unparseable."""
    value = value.strip()
    if not value:
        return 0
    days = 0
    if "-" in value:
        day_part, value = value.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return 0
    try:
        numbers = [int(chunk) for chunk in value.split(":")]
    except ValueError:
        return 0
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    elif len(numbers) == 2:
        hours, minutes, seconds = 0, numbers[0], numbers[1]
    elif len(numbers) == 1:
        hours, minutes, seconds = 0, 0, numbers[0]
    else:
        return 0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_ps_output(text: str) -> list[ProcessInfo]:
    """Parse ``ps -axo pid=,ppid=,etime=,command=`` output."""
    processes: list[ProcessInfo] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, ppid_s, etime_s, command = parts
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
        except ValueError:
            continue
        processes.append(
            ProcessInfo(pid=pid, ppid=ppid, etime_seconds=parse_etime(etime_s), command=command)
        )
    return processes


def is_rehearsal_web_process(proc: ProcessInfo) -> bool:
    """True for a long-running ``serve_rehearsal.py`` server invocation."""
    if "-m py_compile" in proc.command or "py_compile" in proc.command:
        return False
    if "unittest" in proc.command:
        return False
    return any(token.endswith(REHEARSAL_WEB_MARKER) for token in proc.tokens)


def rehearsal_web_tree(proc: ProcessInfo) -> str:
    for token in proc.tokens:
        if token.endswith(REHEARSAL_WEB_MARKER):
            return str(Path(token).resolve().parent)
    return ""


def is_rehearsal_postgres_process(proc: ProcessInfo) -> bool:
    """True only for the rehearsal cluster *master* (keeps ``-D`` path)."""
    if POSTGRES_REHEARSAL_MARKER not in proc.command:
        return False
    tokens = proc.tokens
    if not tokens:
        return False
    if Path(tokens[0]).name != "postgres":
        return False
    return "-D" in tokens


def postgres_rehearsal_data_dir(proc: ProcessInfo) -> str:
    tokens = proc.tokens
    for index, token in enumerate(tokens):
        if token == "-D" and index + 1 < len(tokens):
            return tokens[index + 1]
    return ""


def postgres_rehearsal_port(proc: ProcessInfo) -> int | None:
    tokens = proc.tokens
    for index, token in enumerate(tokens):
        if token == "-p" and index + 1 < len(tokens):
            try:
                return int(tokens[index + 1])
            except ValueError:
                return None
    return None


def classify_web_process(proc: ProcessInfo, by_pid: dict[int, ProcessInfo]) -> str:
    """``active`` when a live parent exists, otherwise ``orphan``.

    A rehearsal server always has a live parent for as long as the gate that
    launched it is running.  When that parent exits the child is reparented to
    PID 1 (or to a non-existent PID), which is the deterministic orphan signal.
    """
    if proc.ppid in (0, 1) or proc.ppid not in by_pid:
        return "orphan"
    return "active"


def is_gate_process(proc: ProcessInfo) -> bool:
    return any(marker in proc.command for marker in GATE_MARKERS)


def temp_purpose(name: str) -> str | None:
    for prefix, purpose in TEMP_PREFIXES.items():
        if name.startswith(prefix):
            return purpose
    return None


def is_worktree_path(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


def classify_temp(
    path: str,
    purpose: str,
    age_seconds: int | None,
    referenced_by: Iterable[int],
    min_age: int,
) -> str:
    if list(referenced_by):
        return "referenced"
    if age_seconds is None:
        return "recent"
    return "stale" if age_seconds >= min_age else "recent"


# --------------------------------------------------------------------------- #
# System probing (thin, injectable)
# --------------------------------------------------------------------------- #

def read_process_table() -> list[ProcessInfo]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,etime=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return parse_ps_output(result.stdout)


def read_process_environ_text() -> str:
    """Raw ``ps eww`` output: command lines plus process environments.

    Gate temp directories are passed to children through environment variables
    (``CRM_DB_PATH``, ``TRADE_OS_WORKBENCH_ENV``, ``SERVICE_LOG``), so reference
    detection must look at environments and not only at ``argv``.
    """
    result = subprocess.run(
        ["ps", "eww", "-axo", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def listening_port(pid: int) -> int | None:
    """Best-effort TCP listen port for *pid* via lsof (None when unavailable)."""
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if not line.startswith("n"):
            continue
        address = line[1:]
        if ":" not in address:
            continue
        try:
            return int(address.rsplit(":", 1)[1])
        except ValueError:
            continue
    return None


def path_age_seconds(path: str, now: float) -> int | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return max(0, int(now - stat.st_mtime))


def _referenced_by(
    path: str,
    processes: list[ProcessInfo],
    self_pid: int,
    environ_text: str = "",
) -> list[int]:
    hits: list[int] = []
    for proc in processes:
        if proc.pid == self_pid:
            continue
        if path in proc.command:
            hits.append(proc.pid)
    if hits:
        return hits
    # Environment-only references (CRM_DB_PATH, TRADE_OS_WORKBENCH_ENV, ...)
    # do not let us name the owning PID reliably, so a sentinel marks "in use".
    if environ_text and path in environ_text:
        hits.append(-1)
    return hits


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #

def scan(
    *,
    processes: list[ProcessInfo] | None = None,
    tmpdir: str | None = None,
    now: float | None = None,
    min_process_age: int = DEFAULT_MIN_PROCESS_AGE,
    min_temp_age: int = DEFAULT_MIN_TEMP_AGE,
    port_lookup: Callable[[int], int | None] = listening_port,
    self_pid: int | None = None,
    environ_text: str | None = None,
) -> dict:
    now = time.time() if now is None else now
    tmpdir = tmpdir if tmpdir is not None else os.environ.get("TMPDIR", "/tmp")
    self_pid = os.getpid() if self_pid is None else self_pid
    system_mode = processes is None
    if processes is None:
        processes = read_process_table()
    if environ_text is None:
        environ_text = read_process_environ_text() if system_mode else ""
    by_pid = {proc.pid: proc for proc in processes}
    gate_running = any(is_gate_process(proc) for proc in processes)

    web: list[WebFinding] = []
    for proc in processes:
        if not is_rehearsal_web_process(proc):
            continue
        status = classify_web_process(proc, by_pid)
        if status == "orphan" and proc.etime_seconds < min_process_age:
            # Brand new; give the launcher a moment before calling it orphaned.
            continue
        web.append(WebFinding(
            pid=proc.pid,
            ppid=proc.ppid,
            tree=rehearsal_web_tree(proc),
            age_seconds=proc.etime_seconds,
            port=port_lookup(proc.pid),
            status=status,
        ))
    web.sort(key=lambda item: item.age_seconds, reverse=True)

    postgres: list[PostgresFinding] = []
    for proc in processes:
        if not is_rehearsal_postgres_process(proc):
            continue
        data_dir = postgres_rehearsal_data_dir(proc)
        port = postgres_rehearsal_port(proc)
        tree = str(Path(data_dir).resolve().parent.parent.parent) if data_dir else ""
        in_use = gate_running
        if not in_use and port is not None:
            # A live rehearsal web server on the same port means the cluster is
            # actively serving an acceptance run.
            in_use = any(item.port == port for item in web)
        postgres.append(PostgresFinding(
            pid=proc.pid,
            tree=tree,
            data_dir=data_dir,
            port=port,
            age_seconds=proc.etime_seconds,
            status="in-use" if in_use else "idle",
        ))
    postgres.sort(key=lambda item: item.age_seconds, reverse=True)

    temp: list[TempFinding] = []
    if os.path.isdir(tmpdir):
        for name in sorted(os.listdir(tmpdir)):
            purpose = temp_purpose(name)
            if purpose is None:
                continue
            path = os.path.join(tmpdir, name)
            age = path_age_seconds(path, now)
            referenced = _referenced_by(path, processes, self_pid, environ_text)
            kind = "file"
            if os.path.isdir(path):
                kind = "worktree" if is_worktree_path(path) else "dir"
            temp.append(TempFinding(
                path=path,
                kind=kind,
                purpose=purpose,
                age_seconds=age,
                referenced_by=referenced,
                status=classify_temp(path, purpose, age, referenced, min_temp_age),
            ))

    return {
        "now": now,
        "tmpdir": tmpdir,
        "gate_running": gate_running,
        "web": [dataclasses.asdict(item) for item in web],
        "postgres": [dataclasses.asdict(item) for item in postgres],
        "temp": [dataclasses.asdict(item) for item in temp],
        "summary": {
            "web_orphans": sum(1 for item in web if item.status == "orphan"),
            "web_active": sum(1 for item in web if item.status == "active"),
            "postgres_idle": sum(1 for item in postgres if item.status == "idle"),
            "temp_stale": sum(1 for item in temp if item.status in {"stale"}),
        },
    }


# --------------------------------------------------------------------------- #
# Clean
# --------------------------------------------------------------------------- #

def build_clean_plan(
    report: dict,
    *,
    include_postgres: bool = False,
    include_worktrees: bool = False,
    min_process_age: int = DEFAULT_MIN_PROCESS_AGE,
) -> list[dict]:
    actions: list[dict] = []
    for item in report["web"]:
        if item["status"] == "orphan" and item["age_seconds"] >= min_process_age:
            actions.append({
                "action": "kill",
                "kind": "web",
                "target": str(item["pid"]),
                "detail": f"pid={item['pid']} tree={item['tree']} port={item['port']}",
            })
    if include_postgres:
        for item in report["postgres"]:
            if item["status"] == "idle":
                actions.append({
                    "action": "stop-postgres",
                    "kind": "postgres",
                    "target": item["data_dir"],
                    "detail": f"pid={item['pid']} tree={item['tree']} port={item['port']}",
                    "tree": item["tree"],
                    "port": item["port"],
                })
    for item in report["temp"]:
        if item["status"] != "stale":
            continue
        if item["kind"] == "worktree" and not include_worktrees:
            continue
        actions.append({
            "action": "remove",
            "kind": item["kind"],
            "target": item["path"],
            "detail": f"{item['purpose']} age={item['age_seconds']}s",
        })
    return actions


def _run(cmd: list[str], env: dict | None = None) -> int:
    return subprocess.run(cmd, capture_output=True, text=True, env=env, check=False).returncode


def _kill_pid(pid: int, *, term_grace: float = TERM_GRACE_SECONDS) -> str:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already-gone"
    except PermissionError:
        return "permission-denied"
    deadline = time.monotonic() + term_grace
    while time.monotonic() < deadline:
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "terminated"
        except PermissionError:
            return "permission-denied"
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except PermissionError:
        return "permission-denied"
    return "killed"


def apply_clean_plan(
    actions: list[dict],
    *,
    python_bin: str | None = None,
    remove_dir: Callable[[str], None] | None = None,
    run: Callable[[list[str], dict | None], int] = _run,
) -> list[dict]:
    import shutil

    remove_dir = remove_dir or (lambda path: shutil.rmtree(path, ignore_errors=True))
    python_bin = python_bin or sys.executable
    results: list[dict] = []
    for action in actions:
        outcome = "done"
        if action["action"] == "kill":
            outcome = _kill_pid(int(action["target"]))
        elif action["action"] == "stop-postgres":
            data_dir = action["target"]
            tree = action.get("tree") or str(Path(data_dir).resolve().parent.parent.parent)
            env = dict(os.environ)
            if action.get("port") is not None:
                env["TROSA_REHEARSAL_PORT"] = str(action["port"])
            tool = os.path.join(tree, "tools", "postgres_rehearsal.py")
            if not os.path.isfile(tool):
                outcome = "missing-tool"
            elif run([python_bin, tool, "stop"], env) != 0:
                outcome = "stop-failed"
        elif action["action"] == "remove":
            target = action["target"]
            if action["kind"] == "worktree":
                removed = False
                for main_root in _candidate_repo_roots():
                    if run(["git", "-C", main_root, "worktree", "remove", "--force", target], None) == 0:
                        removed = True
                        break
                if not removed:
                    remove_dir(target)
            elif action["kind"] == "file":
                try:
                    os.remove(target)
                except OSError:
                    outcome = "remove-failed"
            else:
                remove_dir(target)
        results.append({**action, "outcome": outcome})
    return results


def _candidate_repo_roots() -> list[str]:
    here = Path(__file__).resolve().parents[1]
    return [str(here)]


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def format_report(report: dict) -> str:
    lines: list[str] = []
    summary = report["summary"]
    lines.append(
        "rehearsal hygiene: "
        f"web_orphans={summary['web_orphans']} web_active={summary['web_active']} "
        f"postgres_idle={summary['postgres_idle']} temp_stale={summary['temp_stale']} "
        f"gate_running={str(report['gate_running']).lower()}"
    )
    if report["web"]:
        lines.append("")
        lines.append("rehearsal web processes:")
        for item in report["web"]:
            lines.append(
                f"  [{item['status']:6}] pid={item['pid']:<7} ppid={item['ppid']:<6} "
                f"age={item['age_seconds']}s port={item['port']} tree={item['tree']}"
            )
    if report["postgres"]:
        lines.append("")
        lines.append("rehearsal PostgreSQL clusters:")
        for item in report["postgres"]:
            lines.append(
                f"  [{item['status']:6}] pid={item['pid']:<7} age={item['age_seconds']}s "
                f"port={item['port']} data={item['data_dir']}"
            )
    if report["temp"]:
        lines.append("")
        lines.append("temporary artifacts:")
        for item in report["temp"]:
            refs = ",".join("env" if pid == -1 else str(pid) for pid in item["referenced_by"]) or "-"
            lines.append(
                f"  [{item['status']:10}] {item['kind']:8} age={item['age_seconds']}s "
                f"refs={refs} {item['path']}"
            )
    if not report["web"] and not report["postgres"] and not report["temp"]:
        lines.append("no rehearsal leftovers found")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _scan_common(args) -> dict:
    return scan(
        min_process_age=args.min_process_age,
        min_temp_age=args.min_temp_age,
        tmpdir=args.tmpdir,
    )


def _cmd_scan(args) -> int:
    report = _scan_common(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    if args.check and report["summary"]["web_orphans"]:
        return 2
    return 0


def _cmd_clean(args) -> int:
    report = _scan_common(args)
    plan = build_clean_plan(
        report,
        include_postgres=args.include_postgres,
        include_worktrees=args.include_worktrees,
        min_process_age=args.min_process_age,
    )
    if args.orphans_only:
        plan = [action for action in plan if action["action"] == "kill"]
    if not plan:
        print("rehearsal hygiene: nothing to reclaim")
        return 0
    if not args.apply:
        print(f"rehearsal hygiene: dry run ({len(plan)} action(s)); pass --apply to execute")
        for action in plan:
            print(f"  would {action['action']:13} {action['target']}  ({action['detail']})")
        return 0
    results = apply_clean_plan(plan, python_bin=args.python)
    for result in results:
        print(f"  {result['action']:13} {result['target']}  -> {result['outcome']}")
    failures = [r for r in results if r["outcome"] not in {"done", "terminated", "killed", "already-gone"}]
    print(f"rehearsal hygiene: applied {len(results)} action(s), {len(failures)} unusual")
    return 0


def _cmd_watch(args) -> int:
    print(f"rehearsal hygiene: watching every {args.interval}s (apply={args.apply}); Ctrl-C to stop")
    while True:
        report = _scan_common(args)
        print(format_report(report), flush=True)
        if args.apply:
            plan = build_clean_plan(
                report,
                include_postgres=args.include_postgres,
                include_worktrees=args.include_worktrees,
                min_process_age=args.min_process_age,
            )
            if args.orphans_only:
                plan = [action for action in plan if action["action"] == "kill"]
            if plan:
                results = apply_clean_plan(plan, python_bin=args.python)
                for result in results:
                    print(f"  {result['action']:13} {result['target']}  -> {result['outcome']}", flush=True)
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rehearsal_hygiene.py",
        description="Monitor and safely reclaim orphaned rehearsal processes and temp artifacts.",
    )
    sub = parser.add_subparsers(dest="command")

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--tmpdir", default=None, help="temp root to inspect (default: $TMPDIR)")
        target.add_argument("--min-process-age", type=int, default=DEFAULT_MIN_PROCESS_AGE,
                            help="ignore orphaned web processes younger than this many seconds")
        target.add_argument("--min-temp-age", type=int, default=DEFAULT_MIN_TEMP_AGE,
                            help="only treat unreferenced temp artifacts older than this as stale")
        target.add_argument("--python", default=None, help="project python used for pg rehearsal stop")

    scan_parser = sub.add_parser("scan", help="report leftovers (read-only)")
    add_common(scan_parser)
    scan_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    scan_parser.add_argument("--check", action="store_true",
                             help="exit 2 when orphaned web processes are present")
    scan_parser.set_defaults(func=_cmd_scan)

    clean_parser = sub.add_parser("clean", help="reclaim leftovers (dry run unless --apply)")
    add_common(clean_parser)
    clean_parser.add_argument("--apply", action="store_true", help="actually perform the actions")
    clean_parser.add_argument("--include-postgres", action="store_true",
                              help="also stop idle rehearsal PostgreSQL clusters")
    clean_parser.add_argument("--include-worktrees", action="store_true",
                              help="also remove stale temp git worktrees")
    clean_parser.add_argument("--orphans-only", action="store_true",
                              help="only reclaim orphaned web processes; never touch temp/postgres")
    clean_parser.set_defaults(func=_cmd_clean)

    watch_parser = sub.add_parser("watch", help="monitor continuously")
    add_common(watch_parser)
    watch_parser.add_argument("--interval", type=int, default=60, help="seconds between scans")
    watch_parser.add_argument("--apply", action="store_true", help="reclaim on every scan")
    watch_parser.add_argument("--include-postgres", action="store_true")
    watch_parser.add_argument("--include-worktrees", action="store_true")
    watch_parser.add_argument("--orphans-only", action="store_true")
    watch_parser.set_defaults(func=_cmd_watch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
