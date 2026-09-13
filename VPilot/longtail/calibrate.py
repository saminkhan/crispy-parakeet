#!/usr/bin/env python3
"""Validate the poses and intrinsics of a captured clip.

★ Do this before you capture a night of data, not after. A handedness error or
a wrong FOV->K derivation produces frames that each look perfect and are
mutually inconsistent -- which a world model will happily absorb and which no
amount of eyeballing single frames will reveal.

Two checks, in increasing strength:

1. check_trajectory  -- no images, no opencv. A forward-facing dashcam on a
   moving car must have its camera forward vector aligned with its direction of
   travel. If the Euler convention or the handedness is wrong, this collapses
   immediately. Also flags pose discontinuities.

2. check_epipolar    -- needs opencv. Recovers relative rotation between frame
   pairs from image features using the logged K, and compares it to the relative
   rotation from the logged poses. This validates the intrinsics and the pose
   convention together, end to end.

    python -m longtail.calibrate <clip_dir>
"""

import argparse
import json
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
# VPilot/ for `deepgtav` and `utils`; VPilot/longtail/ for sibling modules.
for _p in (os.path.dirname(_HERE), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import posemath


def load_clip(clip_dir):
    with open(os.path.join(clip_dir, "meta.json")) as f:
        meta = json.load(f)
    poses = []
    with open(os.path.join(clip_dir, "poses.jsonl")) as f:
        for line in f:
            if line.strip():
                poses.append(json.loads(line))
    return meta, poses


# ---------------------------------------------------------------------------
def check_trajectory(poses, min_speed_m=0.25):
    """Forward vector vs direction of travel. Returns a report dict."""
    if len(poses) < 3:
        return {"ok": False, "reason": "too few poses"}

    pos = np.array([p["position"] for p in poses], dtype=np.float64)
    th = np.array([p["theta_deg"] for p in poses], dtype=np.float64)

    cos_errs, steps = [], []
    for i in range(len(poses) - 1):
        d = pos[i + 1] - pos[i]
        n = np.linalg.norm(d)
        steps.append(n)
        if n < min_speed_m:
            continue
        _r, _u, fwd = posemath.camera_basis(th[i])
        cos_errs.append(float(np.dot(d / n, fwd)))

    steps = np.array(steps)
    if not cos_errs:
        return {"ok": False, "reason": "ego never moved far enough to test"}

    cos_errs = np.array(cos_errs)
    median_cos = float(np.median(cos_errs))
    median_angle = float(np.degrees(np.arccos(np.clip(median_cos, -1, 1))))

    # A forward-mounted camera on a car that is driving forwards should sit well
    # under ~25 deg of median misalignment. Near 180 deg means a sign flip; near
    # 90 deg means axes are swapped.
    ok = median_angle < 25.0
    diagnosis = "OK"
    if median_angle > 150:
        diagnosis = "FORWARD VECTOR IS INVERTED (sign flip / handedness error)"
    elif median_angle > 60:
        diagnosis = "AXES LIKELY SWAPPED (check Euler order / basis assignment)"
    elif median_angle >= 25:
        diagnosis = "misaligned -- check camera mount rotation offsets"

    # Discontinuities: a step far above the local median suggests a teleport,
    # a dropped/reordered message, or a clip boundary leaking in.
    med_step = float(np.median(steps)) if len(steps) else 0.0
    jumps = int(np.sum(steps > max(1.0, 8.0 * med_step))) if med_step > 0 else 0

    return {"ok": bool(ok and jumps == 0),
            "median_forward_alignment_deg": round(median_angle, 2),
            "median_cos": round(median_cos, 4),
            "samples": int(len(cos_errs)),
            "median_step_m": round(med_step, 3),
            "pose_jumps": jumps,
            "diagnosis": diagnosis}


# ---------------------------------------------------------------------------
def check_epipolar(clip_dir, meta, poses, max_pairs=20, stride=3):
    """Compare feature-recovered relative rotation against the logged poses."""
    try:
        import cv2
    except ImportError:
        return {"ok": None, "reason": "opencv not installed; skipped"}

    intr = meta.get("intrinsics")
    if not intr:
        return {"ok": False, "reason": "no intrinsics in meta.json"}
    k = np.array([[intr["fx"], 0, intr["cx"]],
                  [0, intr["fy"], intr["cy"]],
                  [0, 0, 1]], dtype=np.float64)

    orb = cv2.ORB_create(4000)
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    errs = []

    idxs = list(range(0, len(poses) - stride, max(1, (len(poses) - stride) // max_pairs)))
    for i in idxs[:max_pairs]:
        pa, pb = poses[i], poses[i + stride]
        ia = cv2.imread(os.path.join(clip_dir, pa["file"]), cv2.IMREAD_GRAYSCALE)
        ib = cv2.imread(os.path.join(clip_dir, pb["file"]), cv2.IMREAD_GRAYSCALE)
        if ia is None or ib is None:
            continue
        ka, da = orb.detectAndCompute(ia, None)
        kb, db = orb.detectAndCompute(ib, None)
        if da is None or db is None or len(ka) < 40 or len(kb) < 40:
            continue
        matches = sorted(bf.match(da, db), key=lambda m: m.distance)[:600]
        if len(matches) < 40:
            continue
        src = np.float32([ka[m.queryIdx].pt for m in matches])
        dst = np.float32([kb[m.trainIdx].pt for m in matches])
        e, mask = cv2.findEssentialMat(src, dst, k, method=cv2.RANSAC, prob=0.999,
                                       threshold=1.0)
        if e is None or e.shape != (3, 3):
            continue
        _n, r_est, _t, _m = cv2.recoverPose(e, src, dst, k, mask=mask)

        # Logged relative rotation, in the same OpenCV camera frame.
        ra = posemath.c2w_opencv(pa["position"], pa["theta_deg"])[:3, :3]
        rb = posemath.c2w_opencv(pb["position"], pb["theta_deg"])[:3, :3]
        r_log = ra.T @ rb                      # a->b in camera frame
        # recoverPose returns the rotation taking frame a into frame b.
        dr = r_est @ r_log
        ang = np.degrees(np.arccos(np.clip((np.trace(dr) - 1) / 2.0, -1, 1)))
        errs.append(float(ang))

    if not errs:
        return {"ok": None, "reason": "no usable frame pairs (texture-poor clip?)"}
    med = float(np.median(errs))
    return {"ok": med < 3.0, "pairs": len(errs),
            "median_rotation_error_deg": round(med, 3),
            "p90_rotation_error_deg": round(float(np.percentile(errs, 90)), 3),
            "diagnosis": ("OK" if med < 3.0 else
                          "intrinsics or pose convention are wrong -- do NOT capture yet")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip_dir")
    ap.add_argument("--skip-epipolar", action="store_true")
    a = ap.parse_args()

    meta, poses = load_clip(a.clip_dir)
    print("clip %s  frames=%d  scenario=%s"
          % (meta.get("clip_id"), meta.get("frames"), meta.get("scenario", {}).get("name")))
    if meta.get("intrinsics"):
        i = meta["intrinsics"]
        print("K: fx=%.2f fy=%.2f cx=%.1f cy=%.1f  (vfov %.2f deg, aspect %.4f)"
              % (i["fx"], i["fy"], i["cx"], i["cy"], i["fov_deg_vertical"],
                 i["aspect_ratio_reported"]))

    t = check_trajectory(poses)
    print("\n[1] trajectory / forward-alignment:")
    for k, v in t.items():
        print("    %-32s %s" % (k, v))

    if not a.skip_epipolar:
        e = check_epipolar(a.clip_dir, meta, poses)
        print("\n[2] epipolar consistency:")
        for k, v in e.items():
            print("    %-32s %s" % (k, v))


if __name__ == "__main__":
    main()
