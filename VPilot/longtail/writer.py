"""Clip writer: frames + per-frame poses + clip metadata, plus quality gates.

Layout per clip:

    <out_dir>/clips/<clip_id>/
        frames/000000.png ...
        poses.jsonl        one JSON object per frame
        meta.json          intrinsics, scenario, environment, provenance

poses.jsonl rows carry the pose that produced *that* frame, read from the same
engine tick as the pixels. Nothing here joins pose to pixels by timestamp -- the
plugin emits them together and we keep them together.
"""

import json
import os

import numpy as np

try:
    import cv2
except ImportError:  # allow import on machines without opencv (tests, analysis)
    cv2 = None
try:
    from PIL import Image as _PILImage
except ImportError:
    _PILImage = None

import posemath


def _thumb(image_bgr, w=96, h=54):
    """Cheap mean-pool downsample.

    Deliberately numpy-only: an earlier version used cv2.resize, so on a machine
    without opencv the pop-in gate silently passed every clip instead of failing
    loudly. Quality gates must not be load-bearing on an optional import.
    """
    a = np.asarray(image_bgr)
    if a.ndim == 2:
        a = a[:, :, None]
    ys = np.linspace(0, a.shape[0], h + 1).astype(int)
    xs = np.linspace(0, a.shape[1], w + 1).astype(int)
    out = np.empty((h, w, a.shape[2]), dtype=np.float32)
    for j in range(h):
        rows = a[ys[j]:max(ys[j] + 1, ys[j + 1])]
        for i in range(w):
            out[j, i] = rows[:, xs[i]:max(xs[i] + 1, xs[i + 1])].mean(axis=(0, 1))
    return out


class ClipWriter:
    def __init__(self, out_dir, clip_id, cfg):
        self.cfg = cfg
        self.clip_id = clip_id
        self.dir = os.path.join(out_dir, "clips", clip_id)
        self.frames_dir = os.path.join(self.dir, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)
        self._pose_fh = open(os.path.join(self.dir, "poses.jsonl"), "w")
        self.n = 0
        self._prev_small = None
        self._prev_pos = None
        self.popin_events = []
        self.travelled = 0.0
        self.first_pose = None
        self.last_pose = None

    # ------------------------------------------------------------------
    def add_frame(self, image_bgr, pose, extra=None):
        """Write one frame + its pose. Returns the frame index."""
        idx = self.n
        ext = self.cfg.image_format
        path = os.path.join(self.frames_dir, "%06d.%s" % (idx, ext))
        if cv2 is not None:
            if ext in ("jpg", "jpeg"):
                cv2.imwrite(path, image_bgr,
                            [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.jpeg_quality])
            else:
                cv2.imwrite(path, image_bgr)
        elif _PILImage is not None:
            # The plugin sends a Windows-bitmap-style buffer, i.e. BGR (which is why
            # cv2.imwrite is correct without a conversion). PIL wants RGB, and
            # fromarray needs a contiguous buffer, so the reversed view is copied.
            rgb = np.ascontiguousarray(image_bgr[:, :, ::-1])
            im = _PILImage.fromarray(rgb)
            if ext in ("jpg", "jpeg"):
                im.save(path, quality=self.cfg.jpeg_quality)
            else:
                im.save(path)
        else:
            raise RuntimeError(
                "need opencv-python or Pillow to write frames; neither is importable")

        row = {"i": idx,
               "file": "frames/%06d.%s" % (idx, ext),
               "game_time_ms": pose["game_time_ms"],
               "position": pose["position"],
               "theta_deg": pose["theta_deg"],
               "fov_deg": pose["fov_deg"]}
        if extra:
            row.update(extra)
        self._pose_fh.write(json.dumps(row) + "\n")

        self._update_quality(image_bgr, pose, idx)
        if self.first_pose is None:
            self.first_pose = pose
        self.last_pose = pose
        self.n += 1
        return idx

    # ------------------------------------------------------------------
    def _update_quality(self, image_bgr, pose, idx):
        """Flag photometric jumps not explained by camera motion.

        That combination is the signature of LOD / asset-streaming pop-in, which
        is geometric inconsistency that looks like plausible geometry to a
        training loss -- the single most damaging artefact for this dataset.
        """
        pos = np.asarray(pose["position"], dtype=np.float64)
        if self._prev_pos is not None:
            self.travelled += float(np.linalg.norm(pos - self._prev_pos))
        if image_bgr is not None:
            small = _thumb(image_bgr)
            if self._prev_small is not None and self._prev_pos is not None:
                photo = float(np.abs(small - self._prev_small).mean())
                motion = float(np.linalg.norm(pos - self._prev_pos))
                if (photo > self.cfg.popin_photometric_threshold
                        and motion < self.cfg.popin_motion_threshold):
                    self.popin_events.append({"frame": idx, "photo_delta": photo,
                                              "motion_m": motion})
            self._prev_small = small
        self._prev_pos = pos

    # ------------------------------------------------------------------
    def finalize(self, meta):
        self._pose_fh.close()

        pose = self.last_pose or self.first_pose
        if pose and pose["fov_deg"] > 0:
            k = posemath.intrinsics(pose["fov_deg"], self.cfg.width, self.cfg.height,
                                    pose["aspect_ratio"])
            meta["intrinsics"] = {
                "model": "PINHOLE",
                "width": self.cfg.width, "height": self.cfg.height,
                "fx": k[0, 0], "fy": k[1, 1], "cx": k[0, 2], "cy": k[1, 2],
                "fov_deg_vertical": pose["fov_deg"],
                "aspect_ratio_reported": pose["aspect_ratio"],
                "near_clip": pose["near_clip"], "far_clip": pose["far_clip"],
                "note": ("fx != fy when the reported aspect ratio differs from "
                         "width/height, which happens in DSR modes. Do not "
                         "assume square pixels."),
            }
        else:
            meta["intrinsics"] = None
            meta.setdefault("warnings", []).append(
                "No CameraFOV in the stream -- the plugin is not built from the "
                "longtail branch, so intrinsics are unknown.")

        meta["frames"] = self.n
        meta["travelled_metres"] = self.travelled
        meta["popin_events"] = self.popin_events
        meta["convention"] = {
            "world_axes": "+X east, +Y north, +Z up (right-handed)",
            "theta_deg": "CAM::GET_CAM_ROT(cam, 0), degrees; R = Rz@Ry@Rx",
            "camera_basis": "forward=R@[0,1,0], up=R@[0,0,1], right=R@[1,0,0]",
            "helper": "longtail.posemath.c2w_opencv(position, theta_deg)",
        }
        with open(os.path.join(self.dir, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2)
        return meta

    # ------------------------------------------------------------------
    def verdict(self, had_collision=False):
        """Should this clip be kept? Returns (keep: bool, reasons: list[str])."""
        reasons = []
        if self.n < 2:
            reasons.append("too few frames")
        # ⚠ Same trap as the stuck detector in episode.py, in a second place: a
        # head-on at 34 m/s stops the ego dead, so the clip travels ~5 m and this
        # gate calls it a stuck spawn. Observed: a `major_collision` at 33.9 m/s
        # entry speed rejected for travelling 5.3 m -- exactly the clip the
        # generator exists to produce. Short travel only means a bad spawn when
        # nothing hit anything.
        if not had_collision and self.travelled < self.cfg.min_travelled_metres:
            reasons.append("ego barely moved (%.1f m) -- likely stuck spawn"
                           % self.travelled)
        # ⚠ This used to reject on ANY pop-in event. Observed: clips discarded for
        # 1-3 flagged frames out of ~350 -- half a batch thrown away over a handful
        # of frames, which is the same mistake as rejecting a 15s clip for 2 bad
        # seconds. A few isolated events are survivable; a clip that is structurally
        # streaming-corrupted shows many. Tolerate up to popin_max_events.
        if self.cfg.popin_reject and len(self.popin_events) > self.cfg.popin_max_events:
            reasons.append("%d pop-in event(s)" % len(self.popin_events))
        return (not reasons), reasons


def _count_video_frames(path):
    """Frames actually in the file.

    ⚠ Do NOT trust the container's nb_frames -- it is routinely absent or wrong on
    VFR output, which is exactly what this encoder produces. Count packets.
    """
    import subprocess
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-count_packets", "-show_entries", "stream=nb_read_packets",
                            "-of", "csv=p=0", path], capture_output=True, text=True)
        return int(r.stdout.strip())
    except Exception:
        return -1


def encode_video(clip_dir, cfg, image_format="png"):
    """Encode a clip's frames to H.264 mp4 at TRUE playback speed.

    ★ Frame intervals are deliberately non-uniform. `slowmo` drops the game's time
    scale to ~0.22 around the trigger, so the half-second that matters is sampled
    ~4.5x more densely -- dt goes from ~33 ms to ~7 ms. That is the point: it is
    where the information is.

    ⚠ This used to encode at ONE constant fps derived from the clip's duration,
    which is wrong in both directions at once. For a 637-frame slow-mo clip the
    derived rate is 42 fps, so the normal section plays 1.4x FAST and the slow-mo
    section 3.4x SLOW -- the clip appears to randomly lurch into slow motion. The
    frames and poses.jsonl were always correct; only the preview lied.

    Each frame is now shown for its own real duration via the concat demuxer, so
    the mp4 plays at true in-game speed regardless of how the sampling varied.
    """
    import json as _json, os as _os, subprocess, glob
    frames_dir = _os.path.join(clip_dir, "frames")
    pose_path = _os.path.join(clip_dir, "poses.jsonl")
    if not _os.path.isdir(frames_dir):
        return None
    files = sorted(glob.glob(_os.path.join(frames_dir, "*." + image_format)))
    n = len(files)
    if n < 2:
        return None

    rows = []
    if _os.path.exists(pose_path):
        rows = [_json.loads(l) for l in open(pose_path) if l.strip()]

    out = _os.path.join(clip_dir, "clip.mp4")
    nominal = float(getattr(cfg, "rate_hz", 20))

    # --- true-timing path -------------------------------------------------
    if len(rows) == n and n > 1:
        t = [r["game_time_ms"] / 1000.0 for r in rows]
        durs = [max(1e-3, t[i + 1] - t[i]) for i in range(n - 1)]
        durs.append(durs[-1])
        listing = _os.path.join(clip_dir, ".frames.concat")
        with open(listing, "w") as f:
            for path, d in zip(files, durs):
                f.write("file '%s'\n" % _os.path.abspath(path).replace("'", "'\\''"))
                f.write("duration %.6f\n" % d)
            # ⚠ concat demuxer quirk: the final entry's duration is ignored unless
            # the file is listed once more -- which makes ffmpeg EMIT it twice, so
            # the mp4 came out with N+1 frames for N poses. Frame k still lined up
            # with pose k, but the file carried a spurious duplicated last frame,
            # and with video_only deleting the JPEGs that is not recoverable later.
            # -frames:v below trims the output back to exactly N.
            f.write("file '%s'\n" % _os.path.abspath(files[-1]).replace("'", "'\\''"))
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", listing,
               # ⚠ -fps_mode needs ffmpeg >= 5.1; -vsync is the spelling that works
               # on both. -r is a CEILING here, not a target: set it above the
               # densest slow-mo sampling (~143 Hz) or those frames get decimated.
               "-vsync", "vfr", "-r", "200",
               "-frames:v", str(n),
               "-c:v", "libx264", "-crf", str(getattr(cfg, "video_crf", 16)),
               "-preset", "medium", "-pix_fmt", "yuv420p", out]
        try:
            subprocess.run(cmd, check=True)
            span = t[-1] - t[0]
            _os.remove(listing)
            got = _count_video_frames(out)
            if got != n:
                # ★ Refuse to return a video that is not frame-for-frame aligned with
                # poses.jsonl. The caller deletes the JPEGs on success, so an encode
                # that silently dropped or duplicated frames would destroy the only
                # copy of the correspondence. Returning None keeps the frames.
                print("[longtail] ALIGNMENT FAIL in %s: mp4 has %d frames, poses has "
                      "%d -- frames kept, video not recorded"
                      % (_os.path.basename(clip_dir), got, n))
                return None
            return {"path": out, "frames": n, "timing": "true",
                    "span_s": round(span, 3),
                    "sampling_hz_min": round(1.0 / max(durs[:-1]), 1),
                    "sampling_hz_max": round(1.0 / min(durs[:-1]), 1),
                    "aligned_with_poses": True,
                    "bytes": _os.path.getsize(out)}
        except Exception as exc:
            print("[longtail] true-timing encode failed (%r); falling back" % (exc,))
            if _os.path.exists(listing):
                _os.remove(listing)

    # --- fallback: constant rate, only when per-frame times are unavailable --
    fps = nominal
    if len(rows) > 1:
        span = (rows[-1]["game_time_ms"] - rows[0]["game_time_ms"]) / 1000.0
        if span > 0.1:
            fps = (len(rows) - 1) / span
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-framerate", "%.6f" % fps,
           "-i", _os.path.join(frames_dir, "%06d." + image_format),
           "-c:v", "libx264", "-crf", str(getattr(cfg, "video_crf", 16)),
           "-preset", "medium", "-pix_fmt", "yuv420p", out]
    try:
        subprocess.run(cmd, check=True)
    except Exception as exc:
        print("[longtail] video encode failed: %r" % (exc,))
        return None
    got = _count_video_frames(out)
    if got != n:
        print("[longtail] ALIGNMENT FAIL in %s: mp4 has %d frames, poses/frames has "
              "%d -- frames kept, video not recorded"
              % (_os.path.basename(clip_dir), got, n))
        return None
    return {"path": out, "fps": round(fps, 3), "frames": n, "timing": "approximate",
            "aligned_with_poses": True, "bytes": _os.path.getsize(out)}
