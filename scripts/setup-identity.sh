#!/usr/bin/env bash
# One-time local setup enforcing the project rule that every commit and push is
# made by the QuantumindSSI account. Run once after cloning:
#
#   ./scripts/setup-identity.sh
#
# It: (1) points git at the versioned .githooks directory, and (2) sets the
# local commit identity to the QuantumindSSI account. The hooks then block any
# commit/push that isn't authored by that account.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# shellcheck source=../.githooks/allowed-identity.sh
. "$REPO_ROOT/.githooks/allowed-identity.sh"

git config core.hooksPath .githooks
git config user.name "$ALLOWED_NAME"
git config user.email "${ALLOWED_EMAILS[0]}"

echo "✓ hooks enabled (core.hooksPath=.githooks)"
echo "✓ commit identity set to: $ALLOWED_NAME <${ALLOWED_EMAILS[0]}>"
echo ""
echo "All commits and pushes must now be made by the QuantumindSSI account."
echo "Push also requires a token with repo write access on QuantumindSSI"
echo "(and the 'workflow' scope for .github/workflows/ changes)."
