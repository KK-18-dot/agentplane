#!/usr/bin/env bash
# A read-only run must leave the seeded repository clean (HANDOFF.md is the only allowed new file).
set -u
w="$1"
dirty="$(git -C "$w" status --porcelain | grep -v 'HANDOFF.md' || true)"
[[ -z "$dirty" ]] || { echo "read-only run modified files: $dirty"; exit 1; }
exit 0
