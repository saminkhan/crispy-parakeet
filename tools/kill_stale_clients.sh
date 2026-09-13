#!/usr/bin/env bash
# Kill orphaned DeepGTAV python clients.
#
# ⚠ Why this matters more than it looks: the plugin's control socket is ZMQ PAIR,
# which accepts exactly ONE peer. An orphan from an aborted run keeps that slot,
# and every later client then connects "successfully", sends Start, and is
# silently ignored -- no error, no messages, indefinitely. It looks identical to
# a broken plugin, and it is not.
#
# ⚠ Deliberately NOT `pkill -f run_generator`: that pattern also matches this
# script's own wrapper shell (the CLAUDE.md hazard). Match the interpreter AND
# the script path, and never match our own PID or parent.
set -u
me=$$; parent=$PPID
found=0
for pid in $(pgrep -x python3 2>/dev/null); do
  [ "$pid" = "$me" ] && continue
  [ "$pid" = "$parent" ] && continue
  cmd="$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)"
  case "$cmd" in
    *longtail/run_generator.py*|*framecheck.py*|*smoke.py*)
      echo "  killing stale client pid=$pid : $(echo "$cmd" | cut -c1-70)"
      kill -TERM "$pid" 2>/dev/null; found=1 ;;
  esac
done
[ "$found" = 0 ] && echo "  no stale clients"
sleep 2
for pid in $(pgrep -x python3 2>/dev/null); do
  cmd="$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)"
  case "$cmd" in *longtail/run_generator.py*|*framecheck.py*)
      echo "  FORCING pid=$pid"; kill -KILL "$pid" 2>/dev/null ;;
  esac
done
echo "  remaining connections to :8000 -> $(ss -tn 2>/dev/null | grep -c ':8000' || echo 0)"
