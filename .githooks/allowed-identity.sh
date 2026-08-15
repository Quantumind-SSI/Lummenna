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
  # If the account also commits under a verified primary email, add it here:
  # "you@yourdomain.example"
)

# Return 0 if $1 (an email) is in the allow-list, else 1.
identity_email_allowed() {
  local candidate="$1"
  local e
  for e in "${ALLOWED_EMAILS[@]}"; do
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
