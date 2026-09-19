#!/usr/bin/env python3
"""Renumber a task's new migrations so parallel tasks cannot collide.

``migrations/`` is the single source of truth and the runtime applies files in
filename order.  Two agents that work in parallel can each add a migration with
the same number but a different name; Git merges that silently, and only the
release gate notices.  Reserving the next number at ``create``/``adopt`` avoids
the common case, but reservation is advisory: a task can ignore it, or another
task can publish a different file into the reserved slot first.

This tool closes that loop.  Before a task rebases or publishes, it:

* lists the migration files the task added relative to its merge-base;
* refuses to touch any migration that already exists in the target tree (main)
  or that is recorded in an applied-migration ledger -- such a file may already
  have run in an environment, so renumbering it is never safe;
* renumbers only the remaining colliding files to fresh numbers, allocating
  those numbers under the same portable lock used by ``create``/``adopt`` and
  from a persistent allocation counter, so two concurrent reconciliation runs
  can never be handed the same number.

Renumbers use ``git mv`` so the change is staged and reviewable; the caller
(sync/publish) commits it.  Non-colliding files are never touched, so a task's
intended ordering is preserved.

The lock protocol mirrors ``deploy/cloud/lib-release-lock.sh`` (``mkdir`` plus
an ``owner`` file with ``<pid> <unix-seconds>``, dead/stale holders reclaimed)
and uses the same ``<meta-dir>/.reserve.lock`` path, so this tool and the shell
entrypoints are mutually exclusive.

Usage:
    python3 tools/reconcile_migrations.py --task-dir DIR \
        [--target-ref origin/main] [--meta-dir DIR] [--task ID] \
        [--applied-ledger FILE] [--apply] [--json]

Exit codes: 0 ok, 1 error, 3 collisions found but not applied (``--apply`` off).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9][a-z0-9_]*\.sql$")
COUNTER_FILE = ".migration-counter"
RESERVE_LOCK = ".reserve.lock"


def migration_number(name: str) -> int | None:
    match = MIGRATION_NAME.match(name)
    return int(match.group(1)) if match else None


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=False
    )


def resolve_commit(repo: str, ref: str) -> str:
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def names_in_tree(
    repo: str, commit: str, subdir: str = "migrations", strict: bool = False
) -> list[str]:
    proc = _git(repo, "ls-tree", "--name-only", f"{commit}:{subdir}")
    if proc.returncode != 0:
        if strict:
            raise SystemExit(
                f"reconcile_migrations: cannot read {subdir}/ of target "
                f"{commit[:9]}; refusing to renumber (fail closed)"
            )
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def added_migrations(task_dir: str, base_sha: str) -> list[str]:
    """Migration basenames the task added relative to ``base_sha``."""
    proc = _git(
        task_dir, "diff", "--name-only", "--diff-filter=A", base_sha, "HEAD",
        "--", "migrations/",
    )
    if proc.returncode != 0:
        return []
    return [os.path.basename(p.strip()) for p in proc.stdout.splitlines() if p.strip()]


def worktree_migration_names(main_root: str) -> list[str]:
    """Every migration basename across the main worktree and all task worktrees."""
    proc = _git(main_root, "worktree", "list", "--porcelain")
    names: list[str] = []
    if proc.returncode != 0:
        return names
    for line in proc.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        path = line[len("worktree "):]
        mdir = os.path.join(path, "migrations")
        if not os.path.isdir(mdir):
            continue
        names.extend(
            os.path.basename(p) for p in sorted(glob.glob(os.path.join(mdir, "*.sql")))
        )
    return names


def reserved_numbers(meta_dir: str, exclude_task: str = "") -> dict[int, str]:
    """``reserved_migration`` values of other active tasks, keyed by number."""
    reserved: dict[int, str] = {}
    if not os.path.isdir(meta_dir):
        return reserved
    for path in sorted(glob.glob(os.path.join(meta_dir, "*.json"))):
        task = os.path.splitext(os.path.basename(path))[0]
        if exclude_task and task == exclude_task:
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            continue
        value = str(doc.get("reserved_migration") or "")
        if value.isdigit():
            reserved[int(value)] = task
    return reserved


def applied_ledger_names(path: str) -> set[str]:
    """Migration basenames recorded as applied by a release/environment ledger."""
    if not path or not os.path.isfile(path):
        return set()
    names: set[str] = set()
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                names.add(os.path.basename(line))
    except OSError:
        return set()
    return names


# --------------------------------------------------------------------------- #
# Portable reservation lock (same protocol as deploy/cloud/lib-release-lock.sh)
# --------------------------------------------------------------------------- #


def _pid_alive(pid: str) -> bool:
    if not pid or not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _remove_lock(lock_dir: str) -> None:
    try:
        owner = os.path.join(lock_dir, "owner")
        if os.path.exists(owner):
            os.remove(owner)
        os.rmdir(lock_dir)
    except OSError:
        pass


def acquire_lock(lock_dir: str, timeout: int = 30, stale: int = 120) -> None:
    """Acquire ``lock_dir`` or raise ``SystemExit`` after ``timeout`` seconds."""
    parent = os.path.dirname(lock_dir)
    if parent:
        os.makedirs(parent, exist_ok=True)
    start = time.time()
    while True:
        try:
            os.mkdir(lock_dir)
        except FileExistsError:
            pass
        else:
            try:
                with open(os.path.join(lock_dir, "owner"), "w", encoding="utf-8") as handle:
                    handle.write(f"{os.getpid()} {int(time.time())}\n")
            except OSError:
                pass
            return
        pid = ts = ""
        try:
            with open(os.path.join(lock_dir, "owner"), encoding="utf-8") as handle:
                parts = handle.read().split()
            if parts:
                pid = parts[0]
                if len(parts) > 1:
                    ts = parts[1]
        except OSError:
            pid = ts = ""
        if pid and not _pid_alive(pid):
            _remove_lock(lock_dir)
            continue
        if ts.isdigit() and int(time.time()) - int(ts) >= stale:
            _remove_lock(lock_dir)
            continue
        if time.time() - start >= timeout:
            raise SystemExit(
                "reconcile_migrations: cannot acquire the migration "
                f"reservation lock {lock_dir} (another task is allocating)"
            )
        time.sleep(0.1)


def release_lock(lock_dir: str) -> None:
    _remove_lock(lock_dir)


def read_counter(meta_dir: str) -> int:
    path = os.path.join(meta_dir, COUNTER_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return 0
    return int(text) if text.isdigit() else 0


def write_counter(meta_dir: str, number: int) -> None:
    path = os.path.join(meta_dir, COUNTER_FILE)
    os.makedirs(meta_dir, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(f"{number:04d}\n")
    os.replace(tmp, path)


def allocate_numbers(
    meta_dir: str, main_root: str, main_names: list[str], count: int
) -> list[int]:
    """Allocate ``count`` fresh migration numbers under the reservation lock.

    The counter is the authority: it never hands a number out twice and never
    recycles one, even when the worktree/meta scan alone would let two tasks
    pick the same first empty slot.  The scan is still combined in so a missing
    or stale counter cannot produce a collision with an existing file.
    """
    if count <= 0:
        return []
    lock_dir = os.path.join(meta_dir, RESERVE_LOCK)
    acquire_lock(lock_dir)
    try:
        used: set[int] = set()
        for name in list(main_names) + worktree_migration_names(main_root):
            number = migration_number(name)
            if number is not None:
                used.add(number)
        used |= set(reserved_numbers(meta_dir).keys())
        nxt = max(used, default=0)
        counter = read_counter(meta_dir)
        if counter > nxt:
            nxt = counter
        out: list[int] = []
        for _ in range(count):
            nxt += 1
            while nxt in used:
                nxt += 1
            used.add(nxt)
            out.append(nxt)
        write_counter(meta_dir, out[-1])
        return out
    finally:
        release_lock(lock_dir)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def colliding_migrations(
    task_added: list[str],
    main_names: list[str],
    machine_names: list[str],
    reserved: dict[int, str],
) -> list[str]:
    """Task-added basenames whose number is already claimed elsewhere.

    A file collides when its number is already claimed by a differently named
    file in the latest main, in another worktree, by another task's
    reservation, or by an earlier file in the same task.  Non-colliding files
    are left exactly as the author wrote them.
    """
    task_names = set(task_added)
    by_number: dict[int, set[str]] = {}
    for name in list(main_names) + list(machine_names):
        number = migration_number(name)
        if number is not None:
            by_number.setdefault(number, set()).add(name)

    chosen: set[int] = set()
    collisions: list[str] = []
    for name in task_added:
        number = migration_number(name)
        if number is None:
            continue
        # Same-task siblings are handled by ``chosen``; anything else sharing
        # the number (main, another worktree) is a real collision.
        others = by_number.get(number, set()) - {name} - task_names
        collides = bool(others) or number in reserved or number in chosen
        if not collides:
            chosen.add(number)
            continue
        collisions.append(name)
    return collisions


def plan_renumbers(
    task_added: list[str],
    main_names: list[str],
    machine_names: list[str],
    reserved: dict[int, str],
) -> list[dict[str, str | int]]:
    """Return ``[{old, new, number}]`` for colliding task-added files.

    This is the pure, lock-free planner used for direct CLI runs without a
    shared meta directory and by unit tests.  The real ``sync``/``publish``
    path allocates through :func:`allocate_numbers` instead.
    """
    occupied: set[int] = set()
    for name in list(main_names) + list(machine_names):
        number = migration_number(name)
        if number is not None:
            occupied.add(number)
    occupied |= set(reserved)

    collisions = colliding_migrations(task_added, main_names, machine_names, reserved)
    chosen: set[int] = set()
    plan: list[dict[str, str | int]] = []
    next_free = max(occupied, default=0) + 1
    for name in collisions:
        while next_free in occupied or next_free in chosen:
            next_free += 1
        new_name = f"{next_free:04d}_{name.split('_', 1)[1]}"
        plan.append({"old": name, "new": new_name, "number": next_free})
        chosen.add(next_free)
        occupied.add(next_free)
        next_free += 1
    return plan


def apply_renames(task_dir: str, plan: list[dict[str, str | int]]) -> list[str]:
    errors: list[str] = []
    for item in plan:
        old = os.path.join("migrations", str(item["old"]))
        new = os.path.join("migrations", str(item["new"]))
        proc = _git(task_dir, "mv", "--", old, new)
        if proc.returncode != 0:
            errors.append(f"{item['old']} -> {item['new']}: {proc.stderr.strip()}")
    return errors


def update_meta_reserved(meta_dir: str, task: str, number: int) -> None:
    path = os.path.join(meta_dir, f"{task}.json")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return
    doc["reserved_migration"] = f"{number:04d}"
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def build_plan(args) -> tuple[list[dict[str, str | int]], dict]:
    task_dir = os.path.abspath(args.task_dir)
    target_sha = resolve_commit(task_dir, args.target_ref)
    if not target_sha:
        raise SystemExit(f"reconcile_migrations: cannot resolve target {args.target_ref}")
    base_proc = _git(task_dir, "merge-base", "HEAD", target_sha)
    base_sha = base_proc.stdout.strip() if base_proc.returncode == 0 else ""
    if not base_sha:
        raise SystemExit(
            f"reconcile_migrations: cannot compute merge-base of HEAD and {args.target_ref}"
        )

    main_root = args.main_root
    if not main_root:
        common = _git(task_dir, "rev-parse", "--git-common-dir")
        if common.returncode == 0 and common.stdout.strip():
            main_root = os.path.dirname(
                os.path.abspath(os.path.join(task_dir, common.stdout.strip()))
            )
    main_root = main_root or task_dir

    task_added = added_migrations(task_dir, base_sha)
    main_names = names_in_tree(task_dir, target_sha, strict=True)
    machine_names = worktree_migration_names(main_root)
    reserved = reserved_numbers(args.meta_dir, args.task) if args.meta_dir else {}

    applied_path = args.applied_ledger
    if not applied_path and args.meta_dir:
        default_ledger = os.path.join(args.meta_dir, ".applied-migrations")
        if os.path.isfile(default_ledger):
            applied_path = default_ledger
    applied = applied_ledger_names(applied_path) if applied_path else set()

    # Safety first: a migration that has entered main, or that a ledger says was
    # applied, must never be renamed.  Entering main is handled silently (the
    # file is no longer "new", so there is nothing to fix); an applied-ledger
    # hit fails closed because we cannot prove the file is still task-local.
    applied_hits = sorted(name for name in task_added if name in applied)
    if applied_hits:
        raise SystemExit(
            "reconcile_migrations: refusing to renumber migration(s) recorded in "
            "the applied ledger: "
            + ", ".join(applied_hits)
            + " (add a new forward migration instead)"
        )
    already_main = sorted(name for name in task_added if name in set(main_names))
    renamable = [name for name in task_added if name not in set(main_names)]

    colliding = colliding_migrations(renamable, main_names, machine_names, reserved)
    if colliding and args.meta_dir:
        numbers = allocate_numbers(args.meta_dir, main_root, main_names, len(colliding))
        plan = [
            {
                "old": name,
                "new": f"{number:04d}_{name.split('_', 1)[1]}",
                "number": number,
            }
            for name, number in zip(colliding, numbers)
        ]
    else:
        plan = plan_renumbers(renamable, main_names, machine_names, reserved)

    summary = {
        "task_dir": task_dir,
        "target_ref": args.target_ref,
        "target_commit": target_sha,
        "base_commit": base_sha,
        "task_added": task_added,
        "already_in_main": already_main,
        "renumbers": plan,
    }
    return plan, summary


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--target-ref", default="origin/main")
    parser.add_argument("--main-root", default="")
    parser.add_argument("--meta-dir", default="")
    parser.add_argument("--task", default="")
    parser.add_argument("--applied-ledger", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv[1:])

    plan, summary = build_plan(args)

    if args.apply and plan:
        errors = apply_renames(summary["task_dir"], plan)
        if errors:
            for error in errors:
                print(f"reconcile_migrations: {error}", file=sys.stderr)
            return 1
        if args.meta_dir and args.task:
            update_meta_reserved(
                args.meta_dir, args.task, max(int(item["number"]) for item in plan)
            )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        if not plan:
            print("RECONCILE_MIGRATIONS_OK no_collision")
        else:
            verb = "renamed" if args.apply else "would_rename"
            for item in plan:
                print(f"RECONCILE_MIGRATIONS_{verb.upper()} {item['old']} -> {item['new']}")

    if plan and not args.apply:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
