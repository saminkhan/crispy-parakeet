#!/usr/bin/env bash
# One line per clip: scenario, outcome, region, frames, off-road %, verdict.
#   clip_summary.sh [OUT_DIR]
set -u
OUT="${1:-/mnt/d/gtav_longtail/continuous}"
python3 - "$OUT" <<'PY'
import json, os, sys, collections
out = sys.argv[1]
root = os.path.join(out, "clips")
if not os.path.isdir(root):
    print("no clips at %s" % root); raise SystemExit(0)
rows, labels, regions, kept = [], collections.Counter(), collections.Counter(), 0
for cid in sorted(os.listdir(root)):
    mp = os.path.join(root, cid, "meta.json")
    if not os.path.exists(mp):
        continue
    m = json.load(open(mp))
    cont = m.get("containment") or {}
    rows.append((cid[:12], m["scenario"]["name"], m["outcome"]["label"],
                 (m.get("placement") or {}).get("region", "?"),
                 m.get("frames") or m.get("n_frames") or 0,
                 100.0 * (cont.get("offroad_fraction") or 0.0),
                 cont.get("road_dist_p95_m"),
                 bool(m.get("keep")),
                 "; ".join(m.get("reject_reasons") or [])[:60]))
    if m.get("keep"):
        kept += 1
        labels[m["outcome"]["label"]] += 1
        regions[(m.get("placement") or {}).get("region", "?")] += 1

print("%-13s %-26s %-17s %-15s %5s %7s %7s %-5s %s" %
      ("clip", "scenario", "outcome", "region", "frm", "offrd%", "p95 m", "keep", "reject"))
for r in rows:
    p95 = "-" if r[6] is None else "%.1f" % r[6]
    print("%-13s %-26s %-17s %-15s %5d %6.1f%% %7s %-5s %s"
          % (r[0], r[1], r[2], r[3], r[4], r[5], p95, r[7], r[8]))
print("\n%d clips written, %d kept (%.0f%%)"
      % (len(rows), kept, 100.0 * kept / max(1, len(rows))))

# ⚠ A set that mixes resampled and native frames has two different effective
# resolutions and two different K's under one name. Say so loudly.
geo = collections.Counter()
for cid in sorted(os.listdir(root)):
    mp = os.path.join(root, cid, "meta.json")
    if not os.path.exists(mp):
        continue
    c = (json.load(open(mp)).get("capture") or {})
    bb = tuple(c.get("backbuffer_wh") or [0, 0])
    fw = tuple(c.get("frame_wh") or [0, 0])
    geo["%dx%d from %s" % (fw[0], fw[1], ("%dx%d" % bb) if bb[0] else "unrecorded")] += 1
if geo:
    print("capture geometry: %s" % dict(geo))
    bad = sum(n for k, n in geo.items() if "unrecorded" not in k and k.split(" from ")[0] != k.split(" from ")[1])
    if bad:
        print("  \u26a0 %d clip(s) RESAMPLED -- their intrinsics describe a grid that does not exist" % bad)
if labels:
    print("outcomes: %s" % dict(labels))
    print("regions : %s" % dict(regions))
PY
