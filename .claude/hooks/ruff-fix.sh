#!/bin/sh
# PostToolUse hook: format and auto-fix a Python file right after Claude edits it,
# so import order, formatting and fixable lint never cost a failed check round.
# Reads the hook JSON on stdin; never blocks the edit (always exits 0).
root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
file=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))')
case "$file" in
  "$root"/*.py) ;;
  *) exit 0 ;;
esac
[ -x "$root/.venv/bin/ruff" ] || exit 0
"$root/.venv/bin/ruff" format --quiet "$file" >/dev/null 2>&1
"$root/.venv/bin/ruff" check --fix --quiet "$file" >/dev/null 2>&1
exit 0
