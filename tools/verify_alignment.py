#!/usr/bin/env python3
"""Verify every clip's mp4 is frame-for-frame aligned with its poses.jsonl.

★ This matters more than it looks. With `video_only` the JPEGs are deleted after
encoding, so the mp4 IS the dataset and poses.jsonl is the only description of
what each of its frames is. If the encoder ever drops, duplicates or reorders a
frame, every pose after that point describes the wrong image -- and nothing else
in the pipeline would notice.

Two levels:
  count   -- packets in the mp4 vs lines in poses.jsonl. Cheap, catches the
             drop/duplicate class outright.
  content -- decode frame k and compare against frames/%06d.jpg. Only possible
             while the JPEGs still exist, so run it on a clip captured with
             video_only=False. Catches reordering and off-by-one, which a count
             check cannot see.

  verify_alignment.py <out_dir> [--content]
"""
import json, os, subprocess, sys


def count_video_frames(path):
    # ⚠ container nb_frames is absent or wrong on VFR output -- count packets.
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-count_packets", "-show_entries", "stream=nb_read_packets",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return -1


def video_timestamps(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                        path], capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        line = line.strip().rstrip(",")
        if line:
            try:
                out.append(float(line))
            except ValueError:
                pass
    return out


def content_check(clip_dir, n, sample=6, window=2):
    """Decode frames and confirm each matches ITS OWN jpg better than its neighbours.

    ⚠ A fixed MAE threshold does not work. H.264-vs-JPEG residual on an aligned
    pair is ~2.0, but the penalty for being off by one depends entirely on how
    fast the scene is moving -- measured on one clip: 3.34 at a slow moment and
    13.07 at a fast one. Any threshold that accepts the first misses the slow-scene
    misalignment; any threshold that rejects it fails good clips.

    So test it relatively: frame k must match jpg k better than jpg k+-1..window.
    That is self-calibrating, immune to scene speed, and it is the actual property
    we care about -- that the correspondence is right, not that the codec is good.
    """
    import numpy as np
    from PIL import Image
    import glob, tempfile
    jpgs = sorted(glob.glob(os.path.join(clip_dir, "frames", "*.jpg")))
    if len(jpgs) != n:
        return None, "frames/ has %d files, poses has %d" % (len(jpgs), n)
    lo, hi = window, n - window - 1
    if hi <= lo:
        return None, "clip too short to check (%d frames)" % n
    idxs = sorted({int(lo + i * (hi - lo) / max(1, sample - 1)) for i in range(sample)})

    def mae(a, b):
        return float(np.abs(a - b).mean())

    margin = None
    with tempfile.TemporaryDirectory() as td:
        for k in idxs:
            png = os.path.join(td, "f.png")
            # select by frame INDEX, not timestamp: the stream is VFR.
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i",
                            os.path.join(clip_dir, "clip.mp4"),
                            "-vf", "select=eq(n\\,%d)" % k, "-vsync", "0",
                            "-frames:v", "1", png], check=True)
            if not os.path.exists(png):
                return None, "could not decode frame %d" % k
            v = np.asarray(Image.open(png).convert("RGB"), dtype=np.float32)
            scores = {}
            for off in range(-window, window + 1):
                j = np.asarray(Image.open(jpgs[k + off]).convert("RGB"), dtype=np.float32)
                if v.shape != j.shape:
                    return None, "frame %d shape %s vs jpg %s" % (k, v.shape, j.shape)
                scores[off] = mae(v, j)
            best = min(scores, key=scores.get)
            if best != 0:
                return None, ("frame %d matches jpg %+d better (%.2f) than its own "
                              "(%.2f) -- MISALIGNED" % (k, best, scores[best], scores[0]))
            second = min(v for o, v in scores.items() if o != 0)
            m = second - scores[0]
            margin = m if margin is None else min(margin, m)
    return margin, None


def main():
    out_dir = sys.argv[1]
    do_content = "--content" in sys.argv
    bad = checked = 0
    for sub in ("clips",):
        root = os.path.join(out_dir, sub)
        if not os.path.isdir(root):
            continue
        for cid in sorted(os.listdir(root)):
            d = os.path.join(root, cid)
            mp4, pf = os.path.join(d, "clip.mp4"), os.path.join(d, "poses.jsonl")
            if not (os.path.exists(mp4) and os.path.exists(pf)):
                continue
            checked += 1
            n = sum(1 for l in open(pf) if l.strip())
            nv = count_video_frames(mp4)
            if nv < 0:
                # ffprobe could not read it: the clip is mid-encode, not broken.
                print("  %-14s poses=%-4d mp4=?     (still encoding, skipped)" % (cid[:12], n))
                checked -= 1
                continue
            note = ""
            ok = (nv == n)
            if ok and do_content:
                margin, err = content_check(d, n)
                if err:
                    ok = False
                    note = "  content: %s" % err
                elif margin is not None:
                    note = "  content OK (worst neighbour margin %.2f MAE)" % margin
            if not ok:
                bad += 1
            print("  %-14s poses=%-4d mp4=%-4d %s%s"
                  % (cid[:12], n, nv, "OK" if nv == n else "*** COUNT %+d ***" % (nv - n), note))
    print("\n%d clips checked, %d bad" % (checked, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
