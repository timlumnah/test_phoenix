#!/bin/bash
# Plays the self-heal demo with typed-out commands. Record it from the repo root with:
#   asciinema rec -q -y --overwrite --cols 110 --rows 34 -c "bash docs/record_demo.sh" docs/demo.cast
#   asciinema-agg --font-size 15 --idle-time-limit 2 docs/demo.cast docs/demo.gif
# Needs the .venv from the README. No API key, no network. Bundles land in prs/ (gitignored).
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
PY=.venv/bin/python
type_cmd() { printf '\033[1;32m$\033[0m '; for ((i=0;i<${#1};i++)); do printf '%s' "${1:i:1}"; sleep 0.035; done; sleep 0.5; echo; }
say() { printf '\033[1;36m# %s\033[0m\n' "$1"; sleep 1.5; }

say "1. The storefront ships a redesign (release v2): same behavior, new markup."
type_cmd "python -m pytest tests -q --tb=no      # suite unedited, run against v2"
RELEASE=v2 $PY -m pytest tests -q --tb=no 2>&1 | grep -v "^$" | tail -4
sleep 2
echo
say "2. phoenix classifies each failure, patches only locators, verifies 3x, refuses regressions."
type_cmd "python -m phoenix.patcher"
$PY -m phoenix.patcher 2>&1 | grep -v "^\[capture\]"
sleep 3
B=$(ls -d prs/selfheal-* | tail -1)
echo
say "3. The result is a reviewable bundle, never an applied change."
type_cmd "cat $B/diff.patch"
cat "$B/diff.patch"
sleep 4
echo
type_cmd "git status --short tests/      # nothing applied; merge is a human command"
git status --short tests/
sleep 3
