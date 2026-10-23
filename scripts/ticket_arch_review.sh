#!/usr/bin/env bash
# Quick architectural-review check for one GitHub issue.
# Usage: scripts/ticket_arch_review.sh <issue-number>
# Prints PASS (review present) or FAIL (missing) with the section names found.
set -euo pipefail
cd "$(dirname "$0")/.."

n="${1:?usage: ticket_arch_review.sh <issue-number>}"
body=$(gh issue view "$n" --json body -q .body 2>/dev/null) || {
  echo "FAIL: issue #$n not found"
  exit 1
}

sections=0
found=""
for s in "Reach" "Design" "Walk-past" "Walkpast" "Architecture" "Approach" "detection gap" "The gap" "Fix" "Mechanism" "Root cause" "Why"; do
  if echo "$body" | grep -qi "$s"; then
    sections=$((sections + 1))
    found="$found $s"
  fi
done

if [ "$sections" -ge 2 ]; then
  echo "PASS: #$n carries an architectural review ($sections sections:$found)"
else
  echo "FAIL: #$n lacks an architectural review (need reach + design/walk-past)"
  exit 1
fi
