#!/usr/bin/env bash
# Shared allow-list of the ONLY identity permitted to author/commit to this
# repository. Sourced by the pre-commit and pre-push hooks.
#
# Policy (project rule): every commit and push to origin must be made by the
# QuantumindSSI account. GitHub attributes a commit to that account only when
# the author/committer email is one of the addresses verified on the account —
# in practice the GitHub-provided noreply address below (or the account's
# verified primary email, added here if you use it).

# Canonical GitHub noreply identity for the QuantumindSSI account (numeric id
# prefix is what GitHub uses to map the commit to the account).
ALLOWED_NAME="QuantumindSSI"
ALLOWED_EMAILS=(
  "277343853+QuantumindSSI@users.noreply.github.com"
  # Account's verified primary email. GitHub stamps this as the author of
  # squash/rebase merges performed in the web UI (e.g. main HEAD b1417cb), all
  # correctly attributed to author.login = QuantumindSSI. Without it, pulling a
  # web-merged commit back into a topic branch would trip this hook.
  "admin@quantumindssi.com"
)

# Committer emails permitted IN ADDITION to the author allow-list. GitHub's
# web merge bot ("web-flow") commits merges as noreply@github.com while the
# author remains the QuantumindSSI account — this is a trusted server-side
# identity, so it is allowed for the committer slot only, never the author.
ALLOWED_COMMITTER_EMAILS=(
  "277343853+QuantumindSSI@users.noreply.github.com"
  "admin@quantumindssi.com"
  "noreply@github.com"
)

# Return 0 if $1 (an email) is an allowed AUTHOR email, else 1.
identity_email_allowed() {
  local candidate="$1"
  local e
  for e in "${ALLOWED_EMAILS[@]}"; do
    [ "$candidate" = "$e" ] && return 0
  done
  return 1
}

# Return 0 if $1 (an email) is an allowed COMMITTER email, else 1. This is a
# superset of the author list that also trusts GitHub's server-side merge bot.
identity_committer_email_allowed() {
  local candidate="$1"
  local e
  for e in "${ALLOWED_COMMITTER_EMAILS[@]}"; do
    [ "$candidate" = "$e" ] && return 0
  done
  return 1
}

# Print the allow-list for error messages.
print_allowed() {
  printf '  name : %s\n' "$ALLOWED_NAME"
  local e
  for e in "${ALLOWED_EMAILS[@]}"; do
    printf '  email: %s\n' "$e"
  done
}
