#!/usr/bin/env bash
# Test suite for bin/carcara. Run: tests/run.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CARCARA="$ROOT/bin/carcara"
PASS=0
FAIL=0
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

newdir() { mktemp -d "$TMP_ROOT/t.XXXXXX"; }
ok() { PASS=$((PASS + 1)); echo "ok   - $1"; }
not_ok() { FAIL=$((FAIL + 1)); echo "FAIL - $1"; }
check() { local name="$1"; shift; if "$@" > /dev/null 2>&1; then ok "$name"; else not_ok "$name"; fi; }
model_of() { sed -n 's/^model: //p' "$1"; }
count_lines() { grep -cxF "$1" "$2"; }

AGENTS="explorer architect implementer test-runner reviewer doc-writer"
COMMANDS="sdlc sdlc-plan sdlc-build sdlc-test sdlc-review"

test_fresh_install() {
  local d; d="$(newdir)"
  "$CARCARA" "$d" > /dev/null || { not_ok "fresh install exits 0"; return; }
  ok "fresh install exits 0"
  local a c missing=0
  for a in $AGENTS; do [ -f "$d/.claude/agents/$a.md" ] || missing=1; done
  for c in $COMMANDS; do [ -f "$d/.claude/commands/$c.md" ] || missing=1; done
  check "all agents and commands installed" [ "$missing" -eq 0 ]
  check "settings.json installed" [ -f "$d/.claude/settings.json" ]
  check "CLAUDE.md created" [ -f "$d/CLAUDE.md" ]
  check "no unrendered placeholders" bash -c "! grep -rq '{{' '$d/.claude' '$d/CLAUDE.md'"
  check "settings.json is valid JSON" python3 -m json.tool "$d/.claude/settings.json"
}

test_agent_frontmatter() {
  local d a f bad=0; d="$(newdir)"
  "$CARCARA" "$d" > /dev/null
  for a in $AGENTS; do
    f="$d/.claude/agents/$a.md"
    [ "$(head -n1 "$f")" = "---" ] || bad=1
    grep -qx "name: $a" "$f" || bad=1
    grep -qE '^description: .+' "$f" || bad=1
    grep -qE '^tools: .+' "$f" || bad=1
    [ -n "$(model_of "$f")" ] || bad=1
  done
  check "agent frontmatter has name/description/tools/model" [ "$bad" -eq 0 ]
}

test_balanced_routing() {
  local d; d="$(newdir)"
  "$CARCARA" "$d" > /dev/null
  check "balanced: architect=opus" [ "$(model_of "$d/.claude/agents/architect.md")" = opus ]
  check "balanced: implementer=sonnet" [ "$(model_of "$d/.claude/agents/implementer.md")" = sonnet ]
  check "balanced: explorer=haiku" [ "$(model_of "$d/.claude/agents/explorer.md")" = haiku ]
  check "balanced: test-runner=haiku" [ "$(model_of "$d/.claude/agents/test-runner.md")" = haiku ]
  check "balanced: main model sonnet" grep -q '"model": "sonnet"' "$d/.claude/settings.json"
  check "balanced: profile named in CLAUDE.md" grep -q 'profile: balanced' "$d/CLAUDE.md"
}

test_other_profiles() {
  local d; d="$(newdir)"
  "$CARCARA" --profile economy "$d" > /dev/null
  check "economy: no opus anywhere" bash -c "! grep -rq 'opus' '$d/.claude'"
  d="$(newdir)"
  "$CARCARA" --profile=quality "$d" > /dev/null
  check "quality: implementer=opus" [ "$(model_of "$d/.claude/agents/implementer.md")" = opus ]
  check "quality: test-runner=haiku" [ "$(model_of "$d/.claude/agents/test-runner.md")" = haiku ]
}

test_custom_profile_file() {
  local d p; d="$(newdir)"; p="$TMP_ROOT/custom.env"
  sed 's/^MODEL_REVIEWER=.*/MODEL_REVIEWER=opus/' "$ROOT/profiles/economy.env" > "$p"
  "$CARCARA" -p "$p" "$d" > /dev/null
  check "custom profile file applied" [ "$(model_of "$d/.claude/agents/reviewer.md")" = opus ]
  check "custom profile named in CLAUDE.md" grep -q 'profile: custom' "$d/CLAUDE.md"
}

test_invalid_input() {
  local d p; d="$(newdir)"
  check "unknown profile fails" bash -c "! '$CARCARA' -p nope '$d' 2>/dev/null"
  check "path-like unknown profile fails" bash -c "! '$CARCARA' -p ../x '$d' 2>/dev/null"
  check "missing target dir fails" bash -c "! '$CARCARA' '$d/missing' 2>/dev/null"
  check "unknown option fails" bash -c "! '$CARCARA' --bogus '$d' 2>/dev/null"
  check "two targets fail" bash -c "! '$CARCARA' '$d' '$d' 2>/dev/null"
  p="$TMP_ROOT/bad.env"
  sed 's/^MODEL_MAIN=.*/MODEL_MAIN=so\/net/' "$ROOT/profiles/balanced.env" > "$p"
  check "unsafe model value rejected" bash -c "! '$CARCARA' -p '$p' '$d' 2>/dev/null"
  grep -v '^MODEL_EXPLORER=' "$ROOT/profiles/balanced.env" > "$p"
  check "incomplete profile rejected" bash -c "! '$CARCARA' -p '$p' '$d' 2>/dev/null"
  check "failed runs wrote nothing" [ ! -e "$d/.claude" ]
}

test_existing_claude_md_preserved_and_idempotent() {
  local d; d="$(newdir)"
  printf '# My project\n\nKeep this line.\n' > "$d/CLAUDE.md"
  "$CARCARA" "$d" > /dev/null
  "$CARCARA" -p quality "$d" > /dev/null
  "$CARCARA" "$d" > /dev/null
  check "user content preserved" grep -qx 'Keep this line.' "$d/CLAUDE.md"
  check "user content first" [ "$(head -n1 "$d/CLAUDE.md")" = "# My project" ]
  check "exactly one begin marker" [ "$(count_lines '<!-- carcara:begin -->' "$d/CLAUDE.md")" -eq 1 ]
  check "exactly one end marker" [ "$(count_lines '<!-- carcara:end -->' "$d/CLAUDE.md")" -eq 1 ]
  check "managed block updated to latest profile" grep -q 'profile: balanced' "$d/CLAUDE.md"
  echo "after" >> "$d/CLAUDE.md"
  "$CARCARA" "$d" > /dev/null
  check "content after block preserved" [ "$(tail -n1 "$d/CLAUDE.md")" = "after" ]
}

test_unbalanced_markers() {
  local d; d="$(newdir)"
  printf '<!-- carcara:begin -->\nx\n' > "$d/CLAUDE.md"
  check "unbalanced markers fail" bash -c "! '$CARCARA' '$d' 2>/dev/null"
  check "unbalanced markers: nothing installed" [ ! -e "$d/.claude" ]
  check "unbalanced CLAUDE.md untouched" [ "$(cat "$d/CLAUDE.md")" = "$(printf '<!-- carcara:begin -->\nx')" ]
  d="$(newdir)"
  printf 'a\n<!-- carcara:end -->\n<!-- carcara:begin -->\nb\n' > "$d/CLAUDE.md"
  check "reversed markers fail" bash -c "! '$CARCARA' '$d' 2>/dev/null"
}

test_skip_and_force() {
  local d; d="$(newdir)"
  mkdir -p "$d/.claude/agents"
  echo "custom" > "$d/.claude/agents/reviewer.md"
  echo '{"model":"opus"}' > "$d/.claude/settings.json"
  local out; out="$("$CARCARA" "$d")"
  check "skip note suggests --force" grep -q 're-run with --force' <<< "$out"
  check "existing agent kept without --force" [ "$(cat "$d/.claude/agents/reviewer.md")" = custom ]
  check "existing settings kept without --force" grep -q '"opus"' "$d/.claude/settings.json"
  check "skip is reported" grep -q 'skip' <<< "$out"
  "$CARCARA" --force "$d" > /dev/null
  check "--force overwrites" grep -qx 'name: reviewer' "$d/.claude/agents/reviewer.md"
}

test_dry_run() {
  local d; d="$(newdir)"
  "$CARCARA" --dry-run "$d" > /dev/null
  check "dry run writes nothing" [ -z "$(ls -A "$d")" ]
  printf 'mine\n' > "$d/CLAUDE.md"
  "$CARCARA" -n "$d" > /dev/null
  check "dry run leaves CLAUDE.md untouched" [ "$(cat "$d/CLAUDE.md")" = mine ]
}

test_default_target_and_symlink() {
  local d l; d="$(newdir)"; l="$TMP_ROOT/bin-link"
  ln -sf "$CARCARA" "$l"
  (cd "$d" && "$l" > /dev/null)
  check "defaults to cwd and works via symlink" [ -f "$d/.claude/agents/explorer.md" ]
}

test_misc_flags() {
  check "--help works" bash -c "'$CARCARA' --help | grep -q Usage"
  check "--version works" bash -c "'$CARCARA' --version | grep -q '^carcara '"
  check "--list-profiles lists all" bash -c "[ \$('$CARCARA' -l | grep -cE '^(economy|balanced|quality)$') -eq 3 ]"
}

for t in $(declare -F | awk '{print $3}' | grep '^test_'); do "$t"; done

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
