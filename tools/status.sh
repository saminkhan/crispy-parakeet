#!/usr/bin/env bash
# One-screen status for a running capture. Read-only.
set -u
OUT="${1:-/mnt/d/gtav_longtail/continuous}"
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

sup=$(ps -eo pid,etimes,args --no-headers | awk '/capture_continuous\.sh/ && !/shell-snapshots/ {printf "pid %s, up %dm", $1, $2/60; exit}')
echo "supervisor : ${sup:-NOT RUNNING}"
echo "game       : $(bash "$R/tools/gta_health.sh" 2>&1 | head -1)"
echo "disk       : $(df -h /mnt/d | tail -1 | awk '{print $4" free ("$5" used)"}')"
echo "chunks     : $(grep -c 'rc=0' "$OUT/continuous.log" 2>/dev/null) ok, $(grep -c 'game DIED' "$OUT/continuous.log" 2>/dev/null) died"
echo
python3 - "$OUT" <<'PY'
import os, sys, json, collections, time
out = sys.argv[1]; root = os.path.join(out, "clips")
if not os.path.isdir(root):
    print("no clips yet"); raise SystemExit
rows = []
for cid in os.listdir(root):
    mp = os.path.join(root, cid, "meta.json")
    if not os.path.exists(mp):
        continue
    try:
        m = json.load(open(mp))
    except Exception:
        continue
    rows.append((os.stat(mp).st_mtime, m))
rows.sort()
kept = [m for _, m in rows if m.get("keep")]
rej = collections.Counter()
for _, m in rows:
    for r in (m.get("reject_reasons") or []):
        rej[r.split("(")[0].strip().split("--")[0].strip()[:44]] += 1
print("clips      : %d written, %d kept (%.0f%%)" % (len(rows), len(kept), 100.0*len(kept)/max(1,len(rows))))
if len(rows) > 1:
    span = rows[-1][0] - rows[0][0]
    recent = [r for r in rows if r[0] > time.time() - 3600]
    print("rate       : %.1f kept/h overall" % (3600.0*len(kept)/max(span,1)), end="")
    if len(recent) > 1:
        rk = sum(1 for r in recent if r[1].get("keep"))
        print("   |   last hour: %d written, %d kept" % (len(recent), rk))
    else:
        print()
    print("last clip  : %.0f min ago" % ((time.time()-rows[-1][0])/60.0))
off = [ (m.get("containment") or {}).get("offroad_fraction") or 0.0 for _, m in rows ]
print("off-road   : max %.1f%% across the set" % (100.0*max(off or [0])))
print("outcomes   : %s" % dict(collections.Counter(m["outcome"]["label"] for m in kept)))
print("regions    : %s" % dict(collections.Counter((m.get("placement") or {}).get("region","?") for m in kept)))
if rej:
    print("rejects    : %s" % "; ".join("%dx %s" % (v,k) for k,v in rej.most_common(4)))
arr = collections.Counter((m.get("timing") or {}).get("arrived_via") for _, m in rows)
if any(arr.values()):
    print("arrival    : %s" % dict(arr))
PY
