#!/usr/bin/env bash
# One-shot health check. Prints "OK ..." / "UNHEALTHY <reason>"; exit 0 / 1.
#
# Non-invasive: never opens the control socket (ZMQ PAIR allows one peer and the
# capture client owns it), only tests that something is bound.
#
# ⚠ `Responding` is reported but NOT treated as fatal. Capture pauses the game
# every frame (SET_GAME_PAUSED + SET_TIME_SCALE 0), so the window stops pumping
# messages and Windows reports "not responding" precisely WHILE the capture is
# working correctly. Failing on it produces an alarm for the entire run.
#
# ⚠ Also note what this can NOT see: a deadlocked script thread. The process
# stays alive, the socket stays bound, and this check stays green while no frames
# are produced at all. Liveness is not progress -- the generator's recv timeout is
# what catches that.
set -u
PS="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
HOST="${DEEPGTAV_HOST:-172.28.32.1}"
PORT="${DEEPGTAV_PORT:-8000}"

read -r proc mem resp <<<"$("$PS" -NoProfile -WindowStyle Hidden -Command '
$p = Get-Process GTA5 -ErrorAction SilentlyContinue
if (-not $p) { "none 0 none" }
else { "{0} {1} {2}" -f $p.ProcessName, [math]::Round($p.WorkingSet64/1GB,2), $p.Responding }
' 2>/dev/null | tr -d '\r')"

[ "$proc" = "none" ] && { echo "UNHEALTHY process-gone"; exit 1; }
if ! timeout 2 bash -c "echo > /dev/tcp/$HOST/$PORT" 2>/dev/null; then
  echo "UNHEALTHY socket-closed mem=${mem}GB responding=${resp}"; exit 1
fi
note=""
[ "$resp" = "False" ] && note=" (window not responding - normal during paused capture)"
echo "OK mem=${mem}GB socket-open responding=${resp}${note}"
