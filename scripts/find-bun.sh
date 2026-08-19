#!/usr/bin/env sh
# Where is bun on this machine? One answer, so every caller gets the same one.
#
# bun's installer writes ~/.bun/bin and appends the PATH export to an interactive
# shell's startup file. Non-interactive shells -- CI steps, subprocess spawns, a
# pytest run -- do not read that file, so "bun is installed" and "bun is on PATH"
# are routinely different facts. A caller that asks PATH alone reports a machine
# that has bun as one that does not, and sends the operator to the installer,
# which writes the same location again.
#
# That was #201 (a test spawning bare argv died with FileNotFoundError while the
# next test over, resolving the same binary its own way, passed) and #206 (the
# import-scan gate told this machine to install what it already had). Both were
# one call site resolving a binary differently from the call site beside it, so
# the fix is one resolver rather than a third spelling of the same two lines.
#
# Exit codes carry the distinction that matters to callers who report rather than
# spawn: ar-runtime/.mcp.json launches its servers with `"command": "bun"`, which
# Claude Code resolves through PATH, so off-PATH is a real failure there and the
# operator needs to hear "fix PATH", not "install bun".
#
#   0  on PATH        -- prints the path; anything that resolves through PATH works
#   3  installed only at ~/.bun/bin -- prints the path; spawning works, PATH does not
#   1  absent         -- prints nothing
#
# Spawning callers want 0 and 3 alike, so they test the output rather than the
# status: BUN="$(scripts/find-bun.sh)"; [ -n "$BUN" ] || ...

set -u

if command -v bun >/dev/null 2>&1; then
    command -v bun
    exit 0
fi

if [ -x "${HOME:-}/.bun/bin/bun" ]; then
    printf '%s\n' "$HOME/.bun/bin/bun"
    exit 3
fi

exit 1
