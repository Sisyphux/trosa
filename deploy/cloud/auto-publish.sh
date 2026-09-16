#!/usr/bin/env bash
# Commit-driven Trosa release entrypoint.
#
# The old file-list form made deployment responsible for Git staging and could
# never safely distinguish one Agent's work from another Agent's dirty files.
# Deployment now accepts only immutable Git inputs and delegates the complete
# candidate/test/publish flow to release-commit.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/release-commit.sh" "$@"
