#!/usr/bin/env python3
"""Production baseline guard for Trosa releases.

A release is only allowed to replace production when the code it ships still
contains everything that production already runs.  Both the deployed artifact
and the release candidate are Git commits, so the rule is a plain ancestry
rule:

    current production commit  must be an ancestor of  the candidate commit
    (or be the very same commit, which is an idempotent re-deploy).

The ECS release runner has no Git history: it downloads a tarball for exactly
one commit.  Ancestry is therefore resolved through the public GitHub compare
API, which is authoritative for the repository the runner already downloads
from.

The guard fails closed.  If the relationship cannot be confirmed -- unknown
production commit, API error, rate limit, malformed response, diverged
history -- the release is refused instead of guessed.  A refused release leaves
production on its last confirmed healthy version.

This module is intentionally stdlib-only: the ECS runner executes it with the
system Python before any release code is imported.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

DEFAULT_API_BASE = "https://api.github.com"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# Compare statuses that prove the candidate still contains production.
_CONTAINS_PRODUCTION = frozenset({"ahead", "identical"})

_NEXT_ACTION_REFUSE = (
    "the candidate was built on an older production state; sync the task to the "
    "latest main, resolve conflicts and publish again"
)


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and bool(SHA_RE.fullmatch(value))


def _decision(allow: bool, reason: str, status: str, production: str,
              candidate: str) -> dict:
    return {
        "allow": allow,
        "reason": reason,
        "status": status,
        "production": production,
        "candidate": candidate,
        "next_action": "" if allow else _NEXT_ACTION_REFUSE,
    }


def evaluate_ancestry(production: object, candidate: object, status: object) -> dict:
    """Pure decision from a resolved compare status.

    ``status`` is the GitHub compare ``status`` for
    ``production...candidate`` (or ``None`` when it could not be resolved).
    Kept separate from the network call so the rule is unit-testable without
    touching the API.
    """
    prod = (production or "").strip().lower() if isinstance(production, str) else ""
    cand = (candidate or "").strip().lower() if isinstance(candidate, str) else ""

    if prod == "none":
        return _decision(True, "no_production", "none", prod, cand)
    if not _valid_sha(cand):
        return _decision(False, "candidate_commit_invalid", "invalid", prod, cand)
    if not _valid_sha(prod):
        return _decision(False, "production_commit_unknown", "unknown", prod, cand)
    if prod == cand:
        return _decision(True, "identical", "identical", prod, cand)
    if status in _CONTAINS_PRODUCTION:
        return _decision(True, f"production_ancestor_{status}", str(status), prod, cand)
    if status is None or status == "":
        return _decision(False, "compare_unavailable", "unavailable", prod, cand)
    return _decision(False, f"stale_baseline_{status}", str(status), prod, cand)


def _http_status(url: str, token: str | None, timeout: float,
                 urlopen) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "trosa-release-baseline",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
        return getattr(response, "status", 200), body


def fetch_compare_status(repository: str, production: str, candidate: str,
                         *, api_base: str = DEFAULT_API_BASE,
                         token: str | None = None, timeout: float = 20.0,
                         urlopen=urllib.request.urlopen) -> str:
    """Return the GitHub compare status for ``production...candidate``.

    Raises on transport or parse failures so callers can fail closed.
    """
    url = (f"{api_base.rstrip('/')}/repos/{repository}/compare/"
           f"{production}...{candidate}")
    _, body = _http_status(url, token, timeout, urlopen)
    doc = json.loads(body)
    status = doc.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("compare response has no status")
    return status


def assess(production: object, candidate: object, repository: str,
           *, api_base: str = DEFAULT_API_BASE, token: str | None = None,
           timeout: float = 20.0, urlopen=urllib.request.urlopen) -> dict:
    """Full guard: resolve ancestry for a release and return a decision dict."""
    prod = (production or "").strip().lower() if isinstance(production, str) else ""
    cand = (candidate or "").strip().lower() if isinstance(candidate, str) else ""
    # Fast, network-free outcomes.
    if prod == "none":
        return evaluate_ancestry(prod, cand, "none")
    if not _valid_sha(prod) or not _valid_sha(cand):
        return evaluate_ancestry(prod, cand, None)
    if prod == cand:
        return evaluate_ancestry(prod, cand, "identical")
    if not REPOSITORY_RE.fullmatch(repository or ""):
        return _decision(False, "repository_invalid", "unknown", prod, cand)
    try:
        status = fetch_compare_status(
            repository, prod, cand, api_base=api_base, token=token,
            timeout=timeout, urlopen=urlopen,
        )
    except Exception:
        # Never guess: transport, auth, rate-limit and 404 all mean "unproven".
        return evaluate_ancestry(prod, cand, None)
    return evaluate_ancestry(prod, cand, status)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True,
                        help="GitHub repository as owner/name")
    parser.add_argument("--production", default="none",
                        help="currently deployed commit, or 'none'")
    parser.add_argument("--candidate", required=True,
                        help="commit about to be deployed")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE,
                        help="GitHub API base (test hook; production keeps the default)")
    args = parser.parse_args(argv[1:])

    token = os.environ.get("TRADE_OS_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    decision = assess(args.production, args.candidate, args.repository,
                      api_base=args.api_base, token=token)
    print(json.dumps(decision, sort_keys=True, separators=(",", ":")))
    return 0 if decision["allow"] else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
