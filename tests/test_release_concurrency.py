"""Production release concurrency-safety regressions.

The failure mode this suite protects against is not "the lock is briefly
contended".  It is that two tasks finish at almost the same time and the second
release was built on a production state that the first release has already
replaced.  A naive lock still lets that release overwrite the other task's
already-live code, silently removing a shipped feature.

The mechanism is now two-layered and this module checks both layers:

* Local (``deploy/cloud/release-commit.sh`` + ``lib-release-lock.sh``): a
  portable, crash-safe lock serializes release candidates, and the release
  candidate is only pushed to ``origin/main`` with ``--force-with-lease`` so a
  stale candidate can never move the branch.

* Production (``deploy/cloud/release-remote.sh`` + ``tools/release_baseline.py``):
  under the ECS ``flock``, before any backup/migration/traffic switch, the
  runner proves that the candidate commit still contains the commit production
  currently runs.  Unproven baselines are refused and production is untouched.

The tests are offline: the GitHub compare API is replaced by a deterministic
history model, so the two-task race is reproducible without ECS, GitHub or a
database.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import release_baseline as baseline  # noqa: E402

LIB = ROOT / "deploy" / "cloud" / "lib-release-lock.sh"
RELEASE_REMOTE = ROOT / "deploy" / "cloud" / "release-remote.sh"
RELEASE_COMMIT = ROOT / "deploy" / "cloud" / "release-commit.sh"
AGENT_WORKTREE = ROOT / "deploy" / "cloud" / "agent-worktree.sh"
TROSA_RELEASE = ROOT / "deploy" / "cloud" / "trosa-release"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def sha(char: str) -> str:
    return char * 40


# --------------------------------------------------------------------------- #
# GitHub compare API stand-in: a linear Git history model.
# --------------------------------------------------------------------------- #


class HistoryModel:
    """Maps a commit id to the set of main commits it contains.

    ``contains`` models ancestry exactly as the GitHub compare API reports it:
    ``prod`` is an ancestor of ``cand`` when everything prod contains is also in
    cand.  This is what the baseline guard must enforce.
    """

    def __init__(self) -> None:
        self._contains: dict[str, set[str]] = {}

    def commit(self, ident: str, *ancestors: str) -> str:
        contained: set[str] = {ident}
        for ancestor in ancestors:
            contained |= self._contains[ancestor]
        self._contains[ident] = contained
        return ident

    def status(self, production: str, candidate: str) -> str:
        if production == candidate:
            return "identical"
        prod = self._contains.get(production, {production})
        cand = self._contains.get(candidate, {candidate})
        if prod <= cand:
            return "ahead"
        if cand <= prod:
            return "behind"
        return "diverged"

    def opener(self):
        """A ``urlopen``-compatible callable returning compare JSON."""

        class Response:
            status = 200

            def __init__(self, body: bytes) -> None:
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def urlopen(request, timeout=None):
            match = re.search(r"/compare/([0-9a-fA-F]+)\.\.\.([0-9a-fA-F]+)$",
                              request.full_url)
            if not match:
                raise ValueError("unexpected compare URL")
            production, candidate = match.group(1), match.group(2)
            body = json.dumps({
                "status": self.status(production, candidate),
                "ahead_by": 1,
            }).encode("utf-8")
            return Response(body)

        return urlopen


class BaselineDecisionTests(unittest.TestCase):
    def test_first_release_has_no_production_to_contain(self):
        decision = baseline.evaluate_ancestry("none", sha("b"), None)
        self.assertTrue(decision["allow"])
        self.assertEqual(decision["reason"], "no_production")

    def test_identical_commit_is_an_idempotent_redeploy(self):
        decision = baseline.evaluate_ancestry(sha("a"), sha("a"), "identical")
        self.assertTrue(decision["allow"])

    def test_candidate_ahead_of_production_is_allowed(self):
        decision = baseline.evaluate_ancestry(sha("a"), sha("b"), "ahead")
        self.assertTrue(decision["allow"])

    def test_stale_candidate_is_refused(self):
        for status in ("behind", "diverged"):
            decision = baseline.evaluate_ancestry(sha("b"), sha("a"), status)
            self.assertFalse(decision["allow"], status)
            self.assertIn("sync", decision["next_action"])

    def test_unknown_production_refuses_instead_of_guessing(self):
        for production in ("unknown", "", None, "not-a-sha"):
            decision = baseline.evaluate_ancestry(production, sha("b"), "ahead")
            self.assertFalse(decision["allow"], production)

    def test_unresolved_compare_refuses(self):
        decision = baseline.evaluate_ancestry(sha("a"), sha("b"), None)
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason"], "compare_unavailable")

    def test_transport_failure_fails_closed(self):
        def boom(request, timeout=None):
            raise OSError("network down")

        decision = baseline.assess(sha("a"), sha("b"), "owner/repo", urlopen=boom)
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason"], "compare_unavailable")

    def test_api_is_not_called_for_production_none(self):
        def forbidden(request, timeout=None):
            raise AssertionError("compare API must not be called")

        decision = baseline.assess("none", sha("b"), "owner/repo", urlopen=forbidden)
        self.assertTrue(decision["allow"])

    def test_invalid_repository_refuses(self):
        decision = baseline.assess(sha("a"), sha("b"), "not-a-repo")
        self.assertFalse(decision["allow"])
        self.assertEqual(decision["reason"], "repository_invalid")


class TwoTaskRaceScenarioTests(unittest.TestCase):
    """The acceptance scenarios, run against the real decision code.

    Both tasks start from the same production commit.  Whichever release lands
    first, the other one must not be able to overwrite it; after the loser
    rebuilds on the winner, production contains both features.
    """

    def setUp(self) -> None:
        self.model = HistoryModel()
        self.p0 = self.model.commit(sha("0"))
        self.a = self.model.commit(sha("a"), self.p0)
        self.b = self.model.commit(sha("b"), self.p0)

    def deploy(self, production: str, candidate: str) -> dict:
        return baseline.assess(production, candidate, "owner/repo",
                               urlopen=self.model.opener())

    def test_first_publisher_wins_and_second_is_refused(self):
        production = self.p0
        first = self.deploy(production, self.a)
        self.assertTrue(first["allow"])
        production = self.a

        stale = self.deploy(production, self.b)
        self.assertFalse(stale["allow"])
        self.assertTrue(stale["reason"].startswith("stale_baseline"), stale)
        # Production did not move backwards: only A shipped.
        self.assertEqual(production, self.a)

    def test_loser_rebuilds_on_winner_and_both_features_ship(self):
        production = self.p0
        self.assertTrue(self.deploy(production, self.a)["allow"])
        production = self.a

        self.assertFalse(self.deploy(production, self.b)["allow"])
        rebuilt = self.model.commit(sha("c"), self.a, self.b)
        decision = self.deploy(production, rebuilt)
        self.assertTrue(decision["allow"], decision)
        production = rebuilt
        self.assertEqual(
            self.model._contains[production] & {self.a, self.b},
            {self.a, self.b},
        )

    def test_reverse_order_never_loses_the_other_feature(self):
        production = self.p0
        self.assertTrue(self.deploy(production, self.b)["allow"])
        production = self.b
        self.assertFalse(self.deploy(production, self.a)["allow"])
        rebuilt = self.model.commit(sha("c"), self.b, self.a)
        self.assertTrue(self.deploy(production, rebuilt)["allow"])
        production = rebuilt
        self.assertEqual(
            self.model._contains[production] & {self.a, self.b},
            {self.a, self.b},
        )

    def test_identical_redeploy_after_partial_failure_is_allowed(self):
        production = self.p0
        self.assertTrue(self.deploy(production, self.a)["allow"])
        production = self.a
        # A re-run of the exact release that is already production.
        self.assertTrue(self.deploy(production, self.a)["allow"])


# --------------------------------------------------------------------------- #
# Portable local lock / migration reservation.
# --------------------------------------------------------------------------- #


LOCK_SCRIPT = r"""
set -euo pipefail
source "__LIB__"
lock="$1" main_root="$2" meta="$3" task="$4" mode="$5"
trosa_lock_acquire "$lock" 20 120 || exit 7
if [ "$mode" = reserve ]; then
  number=$(trosa_next_migration_number "$main_root" "$meta")
  python3 - "$meta/$task.json" "$task" "$number" <<'PY'
import json, sys
path, task, number = sys.argv[1:4]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({"task": task, "reserved_migration": number}, handle)
PY
  trosa_lock_release "$lock"
  printf '%s\n' "$number"
else
  printf 'held\n'
  sleep "${TROSA_LOCK_HOLD_SECONDS:-2}"
  trosa_lock_release "$lock"
fi
""".replace("__LIB__", str(LIB))


class PortableLockTests(unittest.TestCase):
    def run_lock(self, *args, **env):
        base = dict(os.environ)
        base.update(env)
        return subprocess.run(
            ["bash", "-c", LOCK_SCRIPT, "_", *args],
            capture_output=True, text=True, timeout=60, env=base,
        )

    def test_second_holder_is_blocked_and_then_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "t.lock")
            meta = os.path.join(tmp, "metas")
            os.makedirs(meta)
            holder = subprocess.Popen(
                ["bash", "-c", LOCK_SCRIPT, "_", lock, tmp, meta, "h1", "hold"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, "TROSA_LOCK_HOLD_SECONDS": "2"},
            )
            # Wait until the first process owns the lock.
            for _ in range(50):
                if os.path.exists(os.path.join(lock, "owner")):
                    break
                time.sleep(0.05)
            blocked = self.run_lock(lock, tmp, meta, "h2", "reserve")
            _, holder_err = holder.communicate(timeout=30)
            self.assertEqual(holder.returncode, 0, holder_err)
            self.assertEqual(blocked.returncode, 0, blocked.stderr)
            self.assertEqual(blocked.stdout.strip(), "0001")

    def test_dead_owner_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "t.lock")
            meta = os.path.join(tmp, "metas")
            os.makedirs(lock)
            os.makedirs(meta)
            Path(lock, "owner").write_text("999999 1\n", encoding="utf-8")
            result = self.run_lock(lock, tmp, meta, "t", "reserve")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "0001")

    def test_concurrent_reservations_are_unique_and_contiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "reserve.lock")
            meta = os.path.join(tmp, "metas")
            os.makedirs(meta)
            workers = 8
            procs = [
                subprocess.Popen(
                    ["bash", "-c", LOCK_SCRIPT, "_", lock, tmp, meta,
                     f"task{i}", "reserve"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                for i in range(workers)
            ]
            numbers = []
            for proc in procs:
                out, err = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0, err)
                numbers.append(out.strip())
            self.assertEqual(len(set(numbers)), workers, numbers)
            self.assertEqual(sorted(numbers), [f"{n:04d}" for n in range(1, workers + 1)])

    def test_successful_reservation_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = os.path.join(tmp, "t.lock")
            meta = os.path.join(tmp, "metas")
            os.makedirs(meta)
            result = self.run_lock(lock, tmp, meta, "t", "reserve")
            self.assertEqual(result.returncode, 0, result.stderr)
            # Released cleanly, so nothing may linger to wedge the next task.
            self.assertFalse(os.path.exists(lock))


# --------------------------------------------------------------------------- #
# Structural guarantees of the shell entrypoints.
# --------------------------------------------------------------------------- #


class ReleaseRemoteContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = read(RELEASE_REMOTE)

    def test_lock_waits_then_reports_busy_instead_of_hanging(self):
        self.assertIn('flock -w "$LOCK_WAIT" 9', self.script)
        self.assertIn('write_result "busy" "lock"', self.script)
        self.assertIn("MIRROR_LAST_RESULT=0", self.script)
        self.assertIn("exit 75", self.script)

    def test_baseline_guard_runs_under_lock_before_any_mutation(self):
        lock = self.script.index('flock -w "$LOCK_WAIT" 9')
        guard_call = self.script.index("if ! baseline_guard; then")
        migrate = self.script.index("db.init_postgres_store()")
        switch = self.script.index('switch_to "$RELEASE_DIR"')
        backup = self.script.index("pre-migration backup failed")
        self.assertLess(lock, guard_call)
        self.assertLess(guard_call, backup)
        self.assertLess(backup, migrate)
        self.assertLess(migrate, switch)

    def test_baseline_guard_uses_the_shared_helper_and_fails_closed(self):
        self.assertIn("tools/release_baseline.py", self.script)
        self.assertIn('--production "$prod_commit" --candidate "$COMMIT_SHA"', self.script)
        self.assertIn('if [ "$allow" != "True" ]', self.script)
        self.assertIn('write_result "refused" "baseline"', self.script)

    def test_release_id_is_bound_to_one_commit(self):
        self.assertIn("ledger_release_conflict", self.script)
        self.assertIn("already belongs to commit", self.script)
        self.assertIn("is already recorded for commit", self.script)
        self.assertIn('write_result "refused" "identity"', self.script)

    def test_release_ledger_is_append_only_and_serialized(self):
        self.assertIn("append_ledger", self.script)
        self.assertIn(".release-ledger.jsonl", self.script)
        self.assertIn("os.O_APPEND", self.script)

    def test_state_and_result_files_are_written_atomically(self):
        for token in ('atomic_write "$STATE_FILE"', 'atomic_write "$RESULT_FILE"',
                      'atomic_write "$LAST_RESULT"',
                      'atomic_write "$RELEASE_DIR/release.json"'):
            self.assertIn(token, self.script, token)
        self.assertIn("mv -f --", self.script)

    def test_busy_result_does_not_clobber_the_running_release_poll(self):
        marker = self.script.index('write_result "busy" "lock"')
        mirror_off = self.script.rindex("MIRROR_LAST_RESULT=0", 0, marker)
        self.assertGreater(marker, mirror_off)

    def test_rollback_still_shares_the_release_lock(self):
        # Exactly one lock acquisition, before dispatch, so rollback cannot
        # race a deploy either.
        self.assertEqual(self.script.count("flock -w"), 1)
        lock = self.script.index('flock -w "$LOCK_WAIT" 9')
        rollback = self.script.index("do_rollback() {")
        self.assertLess(lock, rollback)


class LocalReleaseContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.commit_script = read(RELEASE_COMMIT)
        self.worktree_script = read(AGENT_WORKTREE)
        self.client = read(TROSA_RELEASE)

    def test_release_commit_uses_shared_common_dir_lock(self):
        self.assertIn("lib-release-lock.sh", self.commit_script)
        self.assertIn("GIT_COMMON_DIR/trosa-release.lock", self.commit_script)
        self.assertIn("trosa_lock_acquire", self.commit_script)
        self.assertIn("trosa_lock_release", self.commit_script)
        # The old, TMPDIR-local, stale-forever lock must be gone.
        self.assertNotIn("trosa-auto-publish.lock", self.commit_script)

    def test_dry_run_does_not_take_the_release_lock(self):
        guard = self.commit_script.index('if [[ "$DRY_RUN" != 1 ]]; then')
        lock = self.commit_script.index("trosa_lock_acquire", guard)
        self.assertLess(guard, lock)

    def test_push_still_refuses_to_overwrite_concurrent_main_moves(self):
        self.assertIn("--force-with-lease=refs/heads/$TARGET_BRANCH:$BASE_SHA",
                      self.commit_script)

    def test_task_worktree_uses_shared_lock_for_reservation(self):
        self.assertIn("lib-release-lock.sh", self.worktree_script)
        self.assertIn("begin_migration_lock", self.worktree_script)
        self.assertIn("release_migration_lock", self.worktree_script)
        self.assertIn("trosa_next_migration_number", self.worktree_script)
        self.assertIn("trap release_migration_lock EXIT", self.worktree_script)

    def test_client_surfaces_busy_as_a_terminal_result(self):
        self.assertIn("TROSA_RELEASE_BUSY", self.client)
        self.assertIn("busy)", self.client)

    def test_scripts_are_shell_valid(self):
        for path in (LIB, RELEASE_REMOTE, RELEASE_COMMIT, AGENT_WORKTREE,
                     TROSA_RELEASE):
            proc = subprocess.run(["bash", "-n", str(path)], capture_output=True,
                                  text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, f"{path}: {proc.stderr}")


class BaselineCliTests(unittest.TestCase):
    def test_first_release_cli_allows_without_network(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "release_baseline.py"),
             "--repository", "owner/repo", "--production", "none",
             "--candidate", sha("b")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["allow"])

    def test_invalid_candidate_is_refused(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "release_baseline.py"),
             "--repository", "owner/repo", "--production", sha("a"),
             "--candidate", "not-a-sha"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(json.loads(proc.stdout)["allow"])


if __name__ == "__main__":
    unittest.main()
