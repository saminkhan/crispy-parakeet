"""Background finalisation of finished clips: encode, prune, record.

★ Finalising a clip takes longer than the capture loop is willing to wait. An
ffmpeg encode of a 15 s clip runs for tens of seconds; the gap the capture loop
leaves between clips is one warmup, ~1-3 s. Encoding inline means the rig spends
more wall clock compressing than driving, and the game sits idle for it. So the
capture loop hands a finished clip to `submit()` and immediately forgets about
it: everything after the last frame -- encode, prune, record -- happens on a
worker while the next clip is already being captured.

Threads, not processes: the slow parts are an ffmpeg subprocess and
`shutil.rmtree`, both of which release the GIL for essentially their whole
duration. The pool is small on purpose -- two concurrent x264 encodes already
saturate the disk, and more workers would only convert a disk bottleneck into
a deeper queue.

⚠ The durable output of this module is the manifest entry, not the files. A
rejected clip's frames are deleted (~250 MB each; a 25% reject rate otherwise
costs as much disk as the dataset itself), but the record of the rejection is
what keeps the reject rate measurable across restarts.
"""

import glob
import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
# longtail's modules import each other flatly (``import posemath``), so both
# VPilot/ and VPilot/longtail/ have to be on the path -- the same two entries
# longtail/__init__.py and run_generator.py add.
for _p in (os.path.join(_REPO, "VPilot"), os.path.join(_REPO, "VPilot", "longtail")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from .manifest import CLIPS_DIRNAME, KEPT_FILES, entry_from_meta, utc_now_iso

#: Pending clips past which submit() complains. Encoding is allowed to lag
#: capture briefly -- a burst of long clips, a slow disk -- but a queue that
#: keeps growing means finalisation is permanently slower than capture, and the
#: frames of every queued clip stay on disk until their turn comes. That is how
#: an overnight run fills the volume while looking healthy.
BACKPRESSURE_PENDING = 4


def _log(msg):
    print("[capture] " + msg, flush=True)


def _get(meta, *path):
    """Dig through nested dicts without assuming any level exists."""
    cur = meta
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _clip_id(clip_dir, meta):
    """⚠ The directory name wins over meta.json's clip_id, not the other way
    round: the manifest addresses a clip as `clips/<clip_id>`, so an id that
    disagrees with the directory files a record pointing at nothing. Same rule
    manifest._scan_clip_dir applies when it adopts a directory."""
    return os.path.basename(os.path.normpath(clip_dir)) or meta.get("clip_id")


def _kept_files(clip_dir):
    """The deliverable files that exist, relative to <output_dir>, and their size.

    Mirrors manifest._scan_clip_dir deliberately: an adopted clip and a finalised
    one must report the same `files` and the same `bytes`, or a restart changes
    the numbers without anything having changed on disk. Note this counts only
    KEPT_FILES -- with keep_frames on, the frames are not part of the delivered
    payload the summary reports.
    """
    rel_root = os.path.join(CLIPS_DIRNAME, os.path.basename(os.path.normpath(clip_dir)))
    files, n_bytes = [], 0
    for name in KEPT_FILES:
        p = os.path.join(clip_dir, name)
        if os.path.exists(p):
            files.append(os.path.join(rel_root, name).replace(os.sep, "/"))
            try:
                n_bytes += os.path.getsize(p)
            except OSError:
                pass
    return files, n_bytes


def _sniff_image_format(clip_dir, meta, cfg):
    """Extension the frames were actually written with.

    ⚠ Sniff the directory before trusting metadata. writer.encode_video globs
    ``frames/*.<image_format>``; hand it the wrong extension and the glob comes
    back empty, encode_video returns None, and a perfectly good clip is filed as
    an alignment failure. The bytes on disk cannot be wrong about their own name;
    a config that was edited between capture and finalise can.
    """
    frames_dir = os.path.join(clip_dir, "frames")
    if os.path.isdir(frames_dir):
        try:
            for name in sorted(os.listdir(frames_dir)):
                ext = os.path.splitext(name)[1].lstrip(".").lower()
                if ext in ("png", "jpg", "jpeg", "bmp"):
                    return ext
        except OSError:
            pass
    return (_get(meta, "capture", "image_format")
            or getattr(cfg, "image_format", None)
            or "png")


class Finalizer(object):
    """Encode / prune / record finished clips off the capture loop's thread.

    Usage is submit-and-forget; `drain()` before shutting the run down so the
    last few clips are not left half-finalised.
    """

    def __init__(self, settings, manifest, workers=2):
        self.settings = settings
        self.manifest = manifest
        self.workers = max(1, int(workers))
        self._pool = ThreadPoolExecutor(max_workers=self.workers)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        # token -> clip_id, so a drain that times out can name what it is waiting on.
        self._inflight = {}
        self._token = 0
        self._closed = False
        self._gen_cfg = None
        self._gen_cfg_tried = False
        self._encode = None
        self._encode_tried = False

    # ------------------------------------------------------------------
    # lazy longtail imports
    #
    # ⚠ Deferred rather than done at construction: writer.py pulls in numpy (and
    # tries cv2/Pillow), and a --dry-run or a settings-validation path has no
    # business requiring those. Cached, because the first import happens on a
    # worker thread and both workers can reach it at once.
    # ------------------------------------------------------------------
    def _encoder(self):
        with self._lock:
            if not self._encode_tried:
                self._encode_tried = True
                try:
                    import writer as _writer
                    self._encode = _writer.encode_video
                except Exception as exc:
                    _log("cannot import longtail writer, clips will not be "
                         "encoded: %r" % (exc,))
            return self._encode

    def _generator_config(self):
        """A longtail CaptureConfig for the encoder.

        encode_video only reads `rate_hz` and `video_crf` off it via getattr, but
        building the real dataclass keeps this honest if the encoder grows a
        third knob.
        """
        with self._lock:
            if not self._gen_cfg_tried:
                self._gen_cfg_tried = True
                try:
                    from config import CaptureConfig
                    self._gen_cfg = CaptureConfig(**self.settings.to_generator_config())
                except Exception as exc:
                    _log("falling back to a minimal encoder config (%r)" % (exc,))
                    self._gen_cfg = _EncodeConfig(self.settings)
            return self._gen_cfg

    # ------------------------------------------------------------------
    def submit(self, clip_dir, meta):
        """Queue a finished clip. Returns immediately; never raises."""
        token = None
        # ★ The clip's creation time is stamped HERE, not when the worker gets to
        # it. submit() is called the moment the last frame lands; finalisation can
        # run a minute later behind a queue, and a timestamp taken there would
        # record when the encoder finished, which is not what "created" means to
        # anyone reading the log back.
        created_utc = utc_now_iso()
        try:
            clip_dir = os.path.abspath(clip_dir)
            # Shallow copy: the caller owns `meta` and may reuse or mutate it as
            # soon as this returns, while the worker is still reading it.
            meta = dict(meta or {})
            with self._cond:
                closed = self._closed
                if not closed:
                    self._token += 1
                    token = self._token
                    self._inflight[token] = _clip_id(clip_dir, meta)
                    pending = len(self._inflight)
            if closed:
                # Should not happen, and losing the record would be worse than
                # the block, so finish it here rather than dropping it.
                _log("submit after close for %s; finalising inline"
                     % _clip_id(clip_dir, meta))
                self._work(None, clip_dir, meta, created_utc)
                return
            if pending > BACKPRESSURE_PENDING:
                _log("WARNING: %d clips pending finalisation (%d workers). "
                     "Encoding is behind capture; unencoded frames are "
                     "accumulating on disk." % (pending, self.workers))
            self._pool.submit(self._work, token, clip_dir, meta, created_utc)
        except Exception as exc:
            # ⚠ submit() is called from the capture loop between clips, so an
            # exception here kills a capture run over bookkeeping. ⚠ The token
            # has to be released too, or a drain() waits forever on a clip that
            # was never queued.
            _log("submit failed for %s: %r" % (clip_dir, exc))
            self._done_token(token)
            try:
                self._work(None, clip_dir, dict(meta or {}), created_utc)
            except Exception as exc2:
                _log("inline finalise also failed for %s: %r" % (clip_dir, exc2))

    # ------------------------------------------------------------------
    def pending(self):
        with self._lock:
            return len(self._inflight)

    def drain(self, timeout=None):
        """Wait for outstanding work. On timeout, report what is still going."""
        deadline = None if timeout is None else time.time() + float(timeout)
        last_note = time.time()
        with self._cond:
            while self._inflight:
                if deadline is None:
                    wait_s = 0.5
                else:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    wait_s = min(0.5, remaining)
                # Waiting in slices rather than one long block so Ctrl-C during a
                # shutdown drain is still responsive and the operator gets told
                # why the process has not exited yet.
                self._cond.wait(wait_s)
                if time.time() - last_note >= 15.0 and self._inflight:
                    last_note = time.time()
                    _log("still finalising %d clip(s): %s"
                         % (len(self._inflight), ", ".join(sorted(self._inflight.values()))))
            stuck = sorted(self._inflight.values())
        if stuck:
            _log("WARNING: drain timed out with %d clip(s) unfinished: %s"
                 % (len(stuck), ", ".join(stuck)))

    def close(self):
        """Drain, then shut the pool down. Safe to call twice."""
        with self._cond:
            already = self._closed
            self._closed = True
        if already:
            return
        self.drain()
        try:
            # ⚠ No cancel_futures: that is 3.9+, and this runs on 3.8.
            self._pool.shutdown(wait=True)
        except Exception as exc:
            _log("pool shutdown: %r" % (exc,))

    # ------------------------------------------------------------------
    def _done_token(self, token):
        with self._cond:
            if token is not None:
                self._inflight.pop(token, None)
            self._cond.notify_all()

    def _record(self, entry):
        # Manifest.record() is documented thread-safe and takes its own lock, so
        # this is not serialised here -- holding the finalizer's lock across an
        # fsync would make submit() and pending() wait on the disk.
        try:
            self.manifest.record(entry)
        except Exception as exc:
            # ⚠ The log is the deliverable that survives everything else; if it
            # cannot be written, put the entry in the run's stdout so the clip is
            # at least recoverable by hand.
            _log("MANIFEST WRITE FAILED for %s (%r); entry was: %s"
                 % (entry.get("clip_id"), exc, json.dumps(entry, default=str)[:600]))

    def _work(self, token, clip_dir, meta, created_utc):
        t0 = time.time()
        try:
            try:
                if meta.get("keep"):
                    entry = self._finalize_kept(clip_dir, meta, created_utc)
                else:
                    entry = self._finalize_rejected(clip_dir, meta, created_utc)
            except Exception as exc:
                # ⚠ One clip's failure must not take the pool down with it, and
                # must not vanish: the files are left exactly where they are and
                # the clip is recorded as not-kept with the error as its reason,
                # so it lands in the reject accounting rather than nowhere.
                _log("finalise FAILED for %s (files left in place): %r"
                     % (_clip_id(clip_dir, meta), exc))
                entry = self._entry(clip_dir, meta, created_utc, kept=False,
                                    extra_reasons=["finalize error: %r" % (exc,)])
                entry["finalize_error"] = repr(exc)
                if os.path.isdir(clip_dir):
                    entry["bytes_on_disk"] = _dir_bytes(clip_dir)
            entry["finalize_s"] = round(time.time() - t0, 2)
            self._record(entry)
        finally:
            self._done_token(token)

    # ------------------------------------------------------------------
    def _finalize_rejected(self, clip_dir, meta, created_utc):
        """Delete the whole clip; keep the fact that it existed and why it went.

        ★ The rejection record is the point. Rejected clips are ~250 MB of frames
        each and cannot be kept, but a reject rate that is not written down is a
        reject rate nobody can measure -- and the reject reasons are what say
        whether the rig is mis-tuned or the world is just being uncooperative.
        """
        clip_id = _clip_id(clip_dir, meta)
        freed = 0
        if os.path.isdir(clip_dir):
            freed = _dir_bytes(clip_dir)
            if not self._safe_to_delete(clip_dir):
                _log("refusing to delete %s: not a directory under %s"
                     % (clip_dir, os.path.join(self.settings.output_dir, CLIPS_DIRNAME)))
                entry = self._entry(clip_dir, meta, created_utc, kept=False)
                entry["bytes_on_disk"] = freed
                return entry
            shutil.rmtree(clip_dir, ignore_errors=True)
        entry = self._entry(clip_dir, meta, created_utc, kept=False)
        entry["pruned"] = True
        entry["bytes_pruned"] = freed
        _log("reject %s (%s) -- freed %.0f MB: %s"
             % (clip_id, entry.get("label") or "?", freed / 1e6,
                "; ".join(entry.get("reject_reasons") or []) or "no reason recorded"))
        return entry

    # ------------------------------------------------------------------
    def _finalize_kept(self, clip_dir, meta, created_utc):
        clip_id = _clip_id(clip_dir, meta)
        if not os.path.isdir(clip_dir):
            return self._entry(clip_dir, meta, created_utc, kept=False,
                               extra_reasons=["clip directory is missing"])

        # ⚠ Defence in depth. settings.to_generator_config() sets video=False so the
        # generator does NOT encode and this Finalizer owns encoding -- that is what
        # keeps ffmpeg off the capture thread. But a clip can still arrive already
        # encoded: adopted from an older run, or captured with a hand-edited config
        # that turned video back on. In that case frames/ is gone, re-encoding finds
        # 0 jpgs, and the clip gets booked as "nothing to encode" -- a GOOD clip
        # recorded as a reject, unrecoverably, because it is already in the log so a
        # later reconcile() skips it.
        #
        # meta["video"] plus the file on disk is the same proof the encode path below
        # waits for: encode_video only returns a dict when the mp4 was verified
        # frame-for-frame against poses.jsonl. Adopt it instead of redoing it.
        existing = meta.get("video") or {}
        mp4_path = os.path.join(clip_dir, "clip.mp4")
        if existing and os.path.exists(mp4_path):
            if not self.settings.keep_frames:
                shutil.rmtree(os.path.join(clip_dir, "frames"), ignore_errors=True)
            return self._entry(clip_dir, meta, created_utc, kept=True)

        cfg = self._generator_config()
        image_format = _sniff_image_format(clip_dir, meta, cfg)
        n_frames = len(glob.glob(os.path.join(clip_dir, "frames", "*." + image_format)))
        encode = self._encoder()

        video = None
        if encode is None:
            reason = "no encoder available (longtail writer did not import)"
        elif n_frames < 2:
            reason = "nothing to encode (%d %s frames on disk)" % (n_frames, image_format)
        else:
            video = encode(clip_dir, cfg, image_format)
            reason = ("clip.mp4 is not frame-for-frame aligned with poses.jsonl "
                      "(encode_video rejected it); frames kept")

        if not video:
            # ★ A None from encode_video means the mp4's frame count did not match
            # poses.jsonl. Frame k must equal pose line k -- that correspondence is
            # the dataset. The frames are the only surviving copy of it, so this is
            # the one failure path that must NOT prune, and the clip is recorded as
            # not-kept so it never counts toward the usable set.
            _log("NOT KEPT %s: %s" % (clip_id, reason))
            entry = self._entry(clip_dir, meta, created_utc, kept=False,
                                extra_reasons=[reason])
            entry["bytes_on_disk"] = _dir_bytes(clip_dir)
            entry["has_frames_dir"] = os.path.isdir(os.path.join(clip_dir, "frames"))
            return entry

        # ⚠ writer.finalize() wrote meta.json before the encode existed, so the
        # video block -- true vs approximate timing, measured sampling range, size
        # -- is otherwise computed and thrown away. Merge into what is on disk
        # rather than dumping the caller's dict: the caller may hold a trimmed
        # copy, and overwriting meta.json with it would destroy the intrinsics,
        # scenario and placement records that only exist in the file.
        self._merge_meta(clip_dir, meta, video)

        keep_frames = bool(getattr(self.settings, "keep_frames", False))
        if not keep_frames:
            shutil.rmtree(os.path.join(clip_dir, "frames"), ignore_errors=True)

        files, n_bytes = _kept_files(clip_dir)
        entry = self._entry(clip_dir, meta, created_utc, kept=True,
                            files=files, n_bytes=n_bytes)
        entry["video"] = {"timing": video.get("timing"),
                          "sampling_hz_min": video.get("sampling_hz_min"),
                          "sampling_hz_max": video.get("sampling_hz_max"),
                          "bytes": video.get("bytes")}
        if keep_frames:
            entry["has_frames_dir"] = True
        _log("keep %s (%s) %d frames, %.1fs, %.0f MB%s"
             % (clip_id, entry.get("label") or "?", entry.get("frames") or 0,
                entry.get("duration_s") or 0.0, n_bytes / 1e6,
                "" if not keep_frames else " (frames kept)"))
        return entry

    # ------------------------------------------------------------------
    def _merge_meta(self, clip_dir, meta, video):
        path = os.path.join(clip_dir, "meta.json")
        on_disk = None
        try:
            with open(path) as f:
                on_disk = json.load(f)
        except Exception:
            on_disk = None
        if not isinstance(on_disk, dict):
            on_disk = dict(meta)
        on_disk["video"] = video
        meta["video"] = video
        tmp = path + ".tmp"
        # Atomic: a crash mid-write would otherwise leave a truncated meta.json,
        # and by this point the frames it describes are about to be deleted.
        with open(tmp, "w") as f:
            json.dump(on_disk, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _safe_to_delete(self, clip_dir):
        """⚠ Guard the rmtree. A malformed clip_dir must not be able to take the
        output tree with it, so only paths that really are `<output>/clips/<id>`
        are deletable."""
        try:
            clips_root = os.path.abspath(
                os.path.join(self.settings.output_dir, CLIPS_DIRNAME))
            parent = os.path.dirname(os.path.abspath(clip_dir))
            return (parent == clips_root
                    and os.path.abspath(clip_dir) != clips_root
                    and bool(os.path.basename(os.path.normpath(clip_dir))))
        except Exception:
            return False

    def _entry(self, clip_dir, meta, created_utc, kept=None, extra_reasons=(),
               files=None, n_bytes=0):
        """One manifest entry, built the same way reconcile() builds an adopted one.

        ★ The schema lives in manifest.entry_from_meta, not here. reconcile()
        calls the same function when it adopts a directory the log never heard
        about, so a clip that was finalised and a clip that was picked up off the
        disk after a crash produce the same fields with the same meanings --
        which is what makes the summary comparable across a restart.

        That includes the counterfactual fields: scene_id and variation are read
        off meta["scene"] in there and ride through untouched. ⚠ Only clip_id is
        taken from the directory (below); a "<scene_id>-<variation>" directory
        name is never split to recover them -- the variation name may contain a
        hyphen, and a clip without a scene block must record null, not a guess.
        """
        entry = entry_from_meta(meta, created_utc=created_utc,
                                files=list(files or []), n_bytes=n_bytes)
        entry["clip_id"] = _clip_id(clip_dir, meta)
        # ★ Prefer the ENCODER's frame count over the one in meta.json. encode_video
        # only returns a dict after verifying the mp4 packet count against
        # poses.jsonl, so video["frames"] is the count actually in the delivered
        # file; meta.json's is whatever the capture loop believed at write time.
        # Where they disagree the file is right.
        vid = (meta.get("video") or {})
        if isinstance(vid, dict) and vid.get("frames"):
            entry["frames"] = int(vid["frames"])
        if isinstance(vid, dict) and vid.get("span_s"):
            entry["duration_s"] = round(float(vid["span_s"]), 3)
        if kept is not None:
            entry["kept"] = bool(kept)
        if extra_reasons:
            entry["reject_reasons"] = (list(entry.get("reject_reasons") or [])
                                       + list(extra_reasons))
        return entry


class _EncodeConfig(object):
    """Last-resort stand-in when a longtail CaptureConfig cannot be built.

    encode_video reads its knobs with getattr defaults, so a clip is still
    encodable with nothing but these three -- better than skipping the encode
    because a settings field drifted.
    """

    def __init__(self, settings):
        self.rate_hz = getattr(settings, "rate_hz", 30)
        self.video_crf = getattr(settings, "video_crf", 16)
        self.image_format = getattr(settings, "image_format", "jpg")
