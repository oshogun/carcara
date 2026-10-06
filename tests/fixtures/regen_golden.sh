#!/usr/bin/env bash
# Regenerate tests/fixtures/golden/ from the installer.
# Usage: tests/fixtures/regen_golden.sh   (CARCARA=<cmd> to override the installer;
# default: the Python CLI from this checkout, `python3 -m carcara`)
set -euo pipefail

FIXTURES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$FIXTURES/../.." && pwd)"
CARCARA="${CARCARA:-python3 -m carcara}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
GOLDEN="$FIXTURES/golden"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# store <installed-dir> <case>: copy output using non-live names.
store() {
  local src="$1" dest="$GOLDEN/$2"
  rm -rf "$dest/dot-claude" "$dest/CLAUDE.md.golden"
  mkdir -p "$dest"
  cp -R "$src/.claude" "$dest/dot-claude"
  cp "$src/CLAUDE.md" "$dest/CLAUDE.md.golden"
}

run_case() {
  local case="$1" profile="$2" input="${3:-}" d="$TMP/$1"
  mkdir -p "$d"
  if [ -n "$input" ]; then
    cp "$input" "$d/CLAUDE.md"
    mkdir -p "$GOLDEN/$case"
    cp "$input" "$GOLDEN/$case/CLAUDE.md.input"
  fi
  # shellcheck disable=SC2086 # CARCARA may be a multi-word command
  $CARCARA -p "$profile" "$d" > /dev/null
  store "$d" "$case"
}

run_case economy economy
run_case balanced balanced
run_case quality quality
run_case custom "$FIXTURES/custom.env"
run_case claude-md-append balanced "$FIXTURES/inputs/claude-md-append.md"
run_case claude-md-update balanced "$FIXTURES/inputs/claude-md-update.md"
echo "golden fixtures regenerated in $GOLDEN"
