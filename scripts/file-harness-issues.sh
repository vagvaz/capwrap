#!/usr/bin/env bash
# File the prepared harness-compatibility issues once `gh` is authenticated.
#
#   gh auth login -h github.com        # token in ~/.config/gh/hosts.yml is 401
#   ./scripts/file-harness-issues.sh   # dry run by default
#   ./scripts/file-harness-issues.sh --yes
#
# Issues go to vagvaz/capwrap (the fork), never upstream (mbailleu/capwrap).
# Nothing is pushed to main.
set -euo pipefail

REPO="vagvaz/capwrap"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRCDIR="${ISSUE_DIR:-/tmp/opencode/gh-issues}"
APPLY=0
[[ "${1:-}" == "--yes" ]] && APPLY=1

if ! gh api user -q .login >/dev/null 2>&1; then
  echo "gh cannot reach the API. Run: gh auth login -h github.com" >&2
  exit 1
fi
echo "authenticated as: $(gh api user -q .login)"

shopt -s nullglob
for f in "$SRCDIR"/0*.md; do
  body="$(awk 'BEGIN{m=0} /^---$/{m++; next} m>=2{print}' "$f")"
  title="$(awk 'BEGIN{m=0} /^---$/{m++; next} m==1 && /^title:/{sub(/^title: */, ""); gsub(/^"|"$/,""); print; exit}' "$f")"
  labels="$(awk 'BEGIN{m=0} /^---$/{m++; next} m==1 && /^labels:/{sub(/^labels: */, ""); print; exit}' "$f" \
    | tr -d '[]"' | tr ',' ' ')"

  # Each label needs its own --label flag; a single flag with a space
  # separated list is rejected by gh as an unknown argument.
  label_args=()
  for l in $labels; do label_args+=(--label "$l"); done

  if [[ $APPLY -eq 1 ]]; then
    echo "→ $title"
    if [[ ${#label_args[@]} -gt 0 ]]; then
      gh issue create --repo "$REPO" --title "$title" --body "$body" "${label_args[@]}" \
        || gh issue create --repo "$REPO" --title "$title" --body "$body"
    else
      gh issue create --repo "$REPO" --title "$title" --body "$body"
    fi
  else
    echo "[dry-run] $title   labels:${labels:- (none)}"
  fi
done

[[ $APPLY -eq 0 ]] && echo; [[ $APPLY -eq 0 ]] && echo "Re-run with --yes to file."
