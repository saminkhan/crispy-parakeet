#!/usr/bin/env bash
# Continuous health monitor, designed to attach to a Monitor without flooding it.
#
# Emission policy:
#   - state CHANGE  -> emit immediately (this is the signal you care about)
#   - still BAD     -> emit with backoff, not every poll. A launch legitimately
#                      takes ~2 min unhealthy; reporting that 8 times is noise,
#                      but going silent through a real outage is worse. So: after
#                      a grace period, report at a decreasing rate and never stop.
#   - still OK      -> silent
#   $1 = poll seconds (default 15)   $2 = grace polls before reporting BAD (default 10)
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INT="${1:-15}"; GRACE="${2:-10}"
last=""; bad=0; next_report=0
while true; do
  if out="$("$HERE/gta_health.sh" 2>&1)"; then state="OK"; else state="BAD"; fi
  if [ "$state" != "$last" ]; then
    echo "[heartbeat $(date +%H:%M:%S)] $out"
    last="$state"; bad=0; next_report="$GRACE"
  elif [ "$state" = "BAD" ]; then
    bad=$((bad+1))
    if [ "$bad" -ge "$next_report" ]; then
      echo "[heartbeat $(date +%H:%M:%S)] still down after $((bad*INT))s: $out"
      next_report=$((bad*2))        # 10, 20, 40 ... polls: never silent, never spam
    fi
  fi
  sleep "$INT"
done
