#!/usr/bin/env bash
# Bring GTA V up in a known-good state: clean slate -> install current plugin ->
# launch -> block until healthy. Exits non-zero (with the ScriptHookV tail) rather
# than leaving a half-dead game for the capture to time out against.
#   $1 = max seconds to wait for health (default 300)
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
G="/mnt/d/SteamLibrary/steamapps/common/Grand Theft Auto V"
ASI="$HERE/../DeepGTAV-PreSIL/bin/Release/DeepGTAV.asi"
DEADLINE="${1:-300}"

# ★ Reuse a healthy game rather than recycling it every chunk.
#
# The bounded-lifetime design existed to make the silent crash cheap. Killing and
# relaunching a HEALTHY game costs ~95 s (cleanup + launch + settle), which at 12
# clips a chunk is ~8 s per clip, so reuse is worth having.
#
# ⚠ An earlier version of this comment said the crash "never happens", on 15
# chunks of evidence. An overnight run then died in 7 of 18 chunks. The crash is
# real and roughly one chunk in three at scale; short runs simply did not sample
# it. Reuse is still right, but ONLY with the progress check below -- reuse based
# on liveness alone made a hung game sticky and cost whole chunks.
#
# ⚠ Only reuse when the RUNNING plugin is the one we just built: an .asi cannot be
# hot-swapped, so a rebuilt plugin still requires a relaunch or the run silently
# captures with stale code. That mistake would be invisible in the output.
#
# The supervisor still detects and recovers from a death, so this trades nothing
# away -- it just stops paying for a crash that is not happening.
if [ "${FORCE_RELAUNCH:-0}" != "1" ] && bash "$HERE/gta_health.sh" >/dev/null 2>&1; then
  if [ -f "$ASI" ] && [ -f "$G/DeepGTAV.asi" ] && cmp -s "$ASI" "$G/DeepGTAV.asi"; then
    # ⚠⚠ gta_health.sh tests LIVENESS, not progress -- its own header warns that a
    # deadlocked script thread keeps it green while no frames are produced. Reusing
    # on that basis is exactly what an overnight run did seven times: each reuse
    # handed back a hung game, the client failed with ZMQ Again('Resource
    # temporarily unavailable'), and the whole chunk produced ZERO clips. Liveness
    # is not progress; require an actual frame before trusting the process.
    bash "$HERE/kill_stale_clients.sh" >/dev/null 2>&1
    # ⚠⚠ NOT `if prog="$(... | tail -1)"`. A pipeline's exit status is the LAST
    # command's, so tail always returns 0 and every failed progress check read as
    # success -- which is the same "reused a hung game" bug this check exists to
    # prevent, reintroduced one layer up in the shell. Capture status separately.
    prog_out="$(bash "$HERE/gta_progress.sh" 20 2>&1)"
    prog_rc=$?
    prog="$(printf '%s' "$prog_out" | tail -1)"
    if [ "$prog_rc" -eq 0 ]; then
      echo "reusing running game: $prog"
      exit 0
    fi
    echo "game is alive but NOT producing frames ($prog) -- relaunching"
  else
    echo "game is healthy but the plugin changed -- relaunching to load it"
  fi
fi

echo "--- cleanup ---"
clean_out="$(bash "$HERE/gta_cleanup.sh")"
echo "$clean_out"
# ⚠ Fail fast instead of launching into a machine that still has GTA5 alive: the
# launcher silently refuses while an instance exists, and we then burn the WHOLE
# health deadline (360 s) discovering it. Observed: 8 minutes lost to one
# survivor. Cheaper to bail and let the caller retry, which re-runs cleanup.
if printf '%s' "$clean_out" | grep -q "STILL RUNNING"; then
  echo "refusing to launch: a previous instance survived cleanup"
  exit 1
fi
# An orphaned client holds the ZMQ PAIR slot and silently blackholes every
# subsequent connection -- clear it before relaunching.
bash "$HERE/kill_stale_clients.sh"

if [ -f "$ASI" ]; then
  cp -f "$ASI" "$G/DeepGTAV.asi" && echo "plugin installed ($(stat -c%s "$G/DeepGTAV.asi") B)"
fi
rm -f "$G/ScriptHookV.log" "$G/asiloader.log"

echo "--- launch ---"
# ⚠ 271590 is "Grand Theft Auto V Legacy". It is NOT the same product as "Grand
# Theft Auto V Enhanced" (app 3240220), which is a separate install that this
# plugin does not support. Override GTA_LAUNCH_CMD for a non-Steam copy, e.g. the
# Rockstar launcher URI or a direct path to PlayGTAV.exe.
LAUNCH="${GTA_LAUNCH_CMD:-steam://rungameid/271590}"
echo "launch target: $LAUNCH"
( cd /mnt/c && "/mnt/c/Windows/System32/cmd.exe" /c start "" "$LAUNCH" >/dev/null 2>&1 )

echo "--- waiting for health (max ${DEADLINE}s) ---"
t=0
while [ "$t" -lt "$DEADLINE" ]; do
  sleep 5; t=$((t+5))
  if out="$(bash "$HERE/gta_health.sh" 2>&1)"; then
    echo "healthy after ${t}s: $out"
    # The socket opens before the world is usable; give the save time to load.
    sleep 45
    echo "world settle done"
    exit 0
  fi
done
echo "TIMEOUT after ${DEADLINE}s: $(bash "$HERE/gta_health.sh" 2>&1)"
echo "--- ScriptHookV tail ---"; tail -6 "$G/ScriptHookV.log" 2>/dev/null
exit 1
