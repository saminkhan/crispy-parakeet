"""The living record of every clip a capture run has produced.

    <output_dir>/manifest.jsonl   one compact JSON object per line, never rewritten
    <output_dir>/summary.json     the derived aggregate, rewritten atomically

★ manifest.jsonl is the artefact that survives everything else: Ctrl-C, a kill -9
mid-write, GTA V crashing and being restarted, the supervisor being restarted, the
box rebooting. summary.json is disposable -- it is recomputed from the log, never
read back as truth. The clips on disk are the payload; the log is what knows they
exist, what is in them, and what was thrown away and why.

Append-only is a durability decision, not a style preference. One line is written,
flushed and fsynced per clip, so the worst a kill -9 can do is leave a torn final
line: load() drops that one line and keeps the other N. A log rewritten in place
loses the whole file to the same kill, and an overnight run is 18 hours of history.

Corrections are appended too, never edited. reconcile() marks a vanished clip with
a second line for the same clip_id, and readers fold the log by clip_id so the last
line wins field by field. What the run believed, and when it believed it, stays in
the file and can be diffed.

Entry schema (the caller builds it; only clip_id is mandatory):

    clip_id         str    the clip directory name under <output_dir>/clips/
    kept            bool   passed the quality gates and its files are on disk
    label           str    outcome label, e.g. "major_collision" (see longtail.outcomes)
    scenario        str    scenario name that produced it
    region          str    coverage region it was sampled from
    duration_s      float  DELIVERED seconds, i.e. excluding the discarded lead
    frames          int    frames in clip.mp4, which equals lines in poses.jsonl
    bytes           int    on-disk size of the files kept
    created_utc     str    ISO-8601 Z, ★ supplied BY THE CALLER (see record())
    reject_reasons  list   why it was dropped; empty for a kept clip
    files           list   paths of the files kept, relative to <output_dir>
    metrics         dict   per-clip numbers to aggregate (chaos measurements)
    pruned          bool   its files were deleted on purpose after rejection
    event           str    "clip" (default), "adopt" or "missing"
    scene_id        str    counterfactual scene the clip belongs to; null without one
    variation       str    variation name within that scene ("ego_chaotic"); null without

Counterfactual variations (`variations` in capture.settings) capture one scene --
same location, conditions, ego vehicle and staged incident -- N times with
different ego/actor behaviour, and name the clips "<scene_id>-<variation>". The
entry carries scene_id and variation so summary() can break outcomes down per
variation ACROSS THE SAME SCENES, which is the comparison the dataset exists for.
★ Both fields come from the clip's own meta.json "scene" block, never from the
directory name: a variation name may itself contain a hyphen, and the id in the
name is a convenience for humans, not the record. The runner also writes

    <output_dir>/scenes.jsonl     one Scene per line, appended as it is proposed

from which summary() reports how many planned scenes are complete. Only
"scene_id" and "n_variations" are read off a line (so a Scene dict dumped as-is
is valid); an optional "variations" list of names makes the check name-exact
rather than a count. It is read with the manifest's tolerance -- a torn line
costs itself, a repeated scene_id folds to its last line.
"""

import argparse
import collections
import datetime
import json
import os
import sys
import threading

MANIFEST_NAME = "manifest.jsonl"
SUMMARY_NAME = "summary.json"
SCENES_NAME = "scenes.jsonl"
CLIPS_DIRNAME = "clips"

#: What a kept clip must leave on disk. reconcile() stats the first path an entry
#: actually claims, so a clip whose directory survived but whose payload did not
#: is still caught.
KEPT_FILES = ("clip.mp4", "poses.jsonl", "meta.json")

#: Sanity limits on marking clips missing. ⚠ The output directory lives on /mnt/d,
#: and an unmounted or not-yet-mounted drive looks exactly like "every clip was
#: deleted". Ratifying that in an append-only log is unrecoverable bookkeeping: the
#: record would permanently claim an entire overnight run is gone. Above these
#: limits reconcile() reports the anomaly and marks nothing.
MISSING_SANITY_FLOOR = 20          # below this many, always believe the disk
MISSING_SANITY_FRACTION = 0.5      # above this share of live clips, refuse

#: Pulled out of a clip's outcome measurements into entry["metrics"], where
#: summary() turns them into distributions. This is the "was the run actually
#: chaotic" panel; the full measurement block stays in the clip's own meta.json.
#: summary() aggregates whatever it finds under "metrics", so adding a key here
#: (or in the caller) is enough to get it reported.
CHAOS_METRICS = ("peak_decel_mps2", "max_abs_roll_deg", "max_speed_mps",
                 "others_damaged_delta", "others_wrecked_max", "others_on_fire_max",
                 "nearby_vehicles_median", "collided", "on_fire", "on_roof")


# ----------------------------------------------------------------------
# helpers


def utc_now_iso():
    """Second-resolution ISO-8601 in UTC, e.g. 2026-09-12T18:04:05Z.

    Zulu rather than +00:00 so the strings sort lexicographically in the same
    order as the instants they name -- summary() relies on that for first/last.
    """
    return (datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"))


def _iso_from_mtime(path):
    try:
        ts = os.path.getmtime(path)
    except OSError:
        return None
    return (datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"))


def _plain(v):
    """Coerce to something json.dumps will accept without raising.

    ⚠ Clip measurements come out of numpy in places (writer.py works in float64),
    and json.dumps raises TypeError on a numpy scalar. Without this a single
    numpy float would lose the clip's manifest line -- i.e. the good clip is on
    disk and the record of it is not. Unwrap via .item() so a number stays a
    number and can still be aggregated; anything genuinely alien becomes a string
    rather than an exception.
    """
    if v is None or isinstance(v, (str, bool, int, float)):
        return v
    if isinstance(v, dict):
        return dict((str(k), _plain(x)) for k, x in v.items())
    if isinstance(v, (list, tuple, set)):
        return [_plain(x) for x in v]
    item = getattr(v, "item", None)
    if callable(item):
        try:
            return _plain(item())
        except Exception:
            pass
    return str(v)


def _as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _pct(sorted_vals, q):
    """Nearest-rank percentile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    k = int(round((q / 100.0) * (len(sorted_vals) - 1)))
    return sorted_vals[max(0, min(len(sorted_vals) - 1, k))]


def _human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % int(n)
        n /= 1024.0


def _human_hms(seconds):
    s = int(round(_as_float(seconds)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh %02dm %02ds" % (h, m, s)
    if m:
        return "%dm %02ds" % (m, s)
    return "%ds" % s


def _short_reason(reason):
    """Collapse a reject reason to its stable prefix so they group.

    Reasons embed measured numbers ("ego barely moved (5.3 m) -- likely stuck
    spawn"), so counting them verbatim gives one bucket per clip. Same
    normalisation tools/status.sh uses, so the two agree.
    """
    return str(reason).split("(")[0].strip().split("--")[0].strip()[:48]


def _fold(entries):
    """Collapse the append-only log to current state: one dict per clip_id.

    Later lines win field by field, so a "missing" correction keeps the label,
    region and byte count the original line carried and only adds the news.
    Insertion order is first-seen order, which is capture order.
    """
    state = collections.OrderedDict()
    for e in entries:
        cid = e.get("clip_id")
        if not cid:
            continue
        cur = state.get(cid)
        if cur is None:
            state[cid] = dict(e)
        else:
            cur.update(e)
    return state


def entry_from_meta(meta, created_utc=None, files=None, n_bytes=None):
    """Build a manifest entry from a longtail clip meta.json dict.

    Kept here rather than in the finalizer so that reconcile() -- which adopts a
    clip directory nobody told the log about -- files it exactly the way the
    finalizer would have.
    """
    meta = meta or {}
    outcome = meta.get("outcome") or {}
    measured = outcome.get("measurements") or {}
    ended = meta.get("ended") or {}
    video = meta.get("video") or {}
    timing = meta.get("timing") or {}

    duration = ended.get("delivered_s")
    if duration is None:
        duration = video.get("span_s")
    if duration is None:
        duration = timing.get("requested_duration_s")

    metrics = dict((k, measured[k]) for k in CHAOS_METRICS if k in measured)
    if "travelled_metres" in meta:
        metrics["travelled_metres"] = meta["travelled_metres"]
    if isinstance(meta.get("popin_events"), list):
        metrics["popin_events"] = len(meta["popin_events"])

    # The generator writes meta["scene"] only when counterfactual variations are
    # on; a single-clip run has no block and both fields stay None, so the entry
    # keeps its previous shape plus two nulls. Absent-or-null is the documented
    # signal for "not part of a scene", so an empty string is folded to None too.
    scene_id, variation = None, None
    scene = meta.get("scene")
    if isinstance(scene, dict):
        scene_id = scene.get("scene_id") or None
        var = scene.get("variation")
        if isinstance(var, dict):
            variation = var.get("name") or None
        elif isinstance(var, str):
            variation = var or None

    entry = {
        "clip_id": meta.get("clip_id"),
        "kept": bool(meta.get("keep")),
        "label": outcome.get("label"),
        "scenario": (meta.get("scenario") or {}).get("name"),
        "region": (meta.get("placement") or {}).get("region"),
        "duration_s": _as_float(duration),
        "frames": _as_int(meta.get("frames")),
        "bytes": _as_int(n_bytes),
        "created_utc": created_utc,
        "reject_reasons": list(meta.get("reject_reasons") or []),
        "files": list(files or []),
        "metrics": metrics,
        "scene_id": scene_id,
        "variation": variation,
    }
    return entry


# ----------------------------------------------------------------------


class Manifest(object):
    """Append-only clip log for one output directory.

    Thread-safe: the finalizer records from its worker threads while the capture
    loop is already on the next clip. Cross-process appends are safe too -- each
    line is a single small O_APPEND write, which the kernel does not interleave --
    so `python3 -m capture.manifest <dir>` can read a live run, and a second
    supervisor appending to the same log will not corrupt it.
    """

    def __init__(self, output_dir):
        self.output_dir = os.path.abspath(output_dir)
        self.path = os.path.join(self.output_dir, MANIFEST_NAME)
        self.summary_path = os.path.join(self.output_dir, SUMMARY_NAME)
        self.scenes_path = os.path.join(self.output_dir, SCENES_NAME)
        self.clips_dir = os.path.join(self.output_dir, CLIPS_DIRNAME)
        self._lock = threading.RLock()
        #: Bookkeeping from the last load(), so a caller can report log damage.
        self.total_lines = 0
        self.skipped_lines = 0
        self.truncated_lines = 0
        self.torn_tail = False
        self._warned_skipped = -1
        self._fsync_warned = False
        self._cache_key = None
        self._cache_text = ""

    # -- writing -------------------------------------------------------
    def record(self, entry):
        """Append one entry. Durable before it returns.

        ⚠ created_utc is the CALLER's to supply. Finalizing happens in the
        background, so a clip can be recorded a minute or more after it was
        captured; a timestamp taken here would be the time the encoder finished,
        which is not what anyone reading the log will assume it means.
        """
        if not isinstance(entry, dict):
            raise TypeError("manifest entry must be a dict, got %r" % type(entry).__name__)
        clip_id = entry.get("clip_id")
        if not clip_id:
            raise ValueError("manifest entry needs a clip_id; got keys %s"
                             % sorted(entry.keys()))

        # clip_id and event lead the line so a torn tail is still identifiable by
        # eye, and `grep '"event":"missing"'` works without a JSON parser.
        row = collections.OrderedDict()
        row["clip_id"] = str(clip_id)
        row["event"] = str(entry.get("event") or "clip")
        for k, v in entry.items():
            if k in ("clip_id", "event"):
                continue
            row[k] = _plain(v)
        if "kept" in row:
            row["kept"] = bool(row["kept"])
        if "frames" in row:
            row["frames"] = _as_int(row["frames"])
        if "bytes" in row:
            row["bytes"] = _as_int(row["bytes"])
        if "duration_s" in row:
            row["duration_s"] = round(_as_float(row["duration_s"]), 3)
        if "reject_reasons" in row and not isinstance(row["reject_reasons"], list):
            row["reject_reasons"] = [str(row["reject_reasons"])]

        # ensure_ascii escapes any newline inside a string, so no value a caller
        # passes can split its own line in two.
        line = json.dumps(row, separators=(",", ":")) + "\n"
        with self._lock:
            self._append(line)

    def _append(self, line):
        if not os.path.isdir(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)
        # ⚠ Heal a torn tail BEFORE appending. A kill -9 mid-write leaves a final
        # line with no newline, and appending straight onto it fuses the fragment
        # and the new entry into one unparseable line -- so the restart silently
        # loses its FIRST clip as well as the interrupted one, which is the pair of
        # clips anyone investigating the crash most wants. Measured on a truncated
        # log: two entries in, zero entries out. "a+" so the last byte is readable;
        # writes still go to the true end of file regardless of the read cursor.
        # Binary, because a text-mode seek offset is an opaque cookie rather than
        # a byte count, and because a line-oriented log must never have its
        # newlines translated by the platform.
        with open(self.path, "ab+") as f:
            end = f.seek(0, os.SEEK_END)
            if end:
                f.seek(end - 1)
                if f.read(1) != b"\n":
                    f.write(b"\n")
            f.write(line.encode("utf-8"))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError as exc:
                # flush() already put the bytes in the page cache, which is what
                # survives the process being killed -- the threat this log exists
                # for. fsync buys survival of a host crash and is not worth
                # aborting a capture run over if the filesystem refuses it.
                if not self._fsync_warned:
                    self._fsync_warned = True
                    print("[capture] manifest fsync unavailable (%s); "
                          "entries are flushed but not synced" % exc)
        self._cache_key = None

    # -- reading -------------------------------------------------------
    def _read_text(self):
        """Raw log text, cached on (size, mtime).

        The log is append-only, so size alone changes on every write; reading a
        few thousand lines off /mnt/d costs far more than parsing them, and
        summary() is recomputed after every clip.
        """
        try:
            st = os.stat(self.path)
        except OSError:
            self._cache_key, self._cache_text = None, ""
            return ""
        key = (st.st_size, st.st_mtime_ns)
        if key != self._cache_key:
            # errors="replace": a damaged byte should cost one line at parse
            # time, not raise out of every read of the whole log.
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                self._cache_text = f.read()
            self._cache_key = key
        return self._cache_text

    def load(self):
        """Every entry in log order. Never raises on a damaged log.

        ⚠ A kill -9 during the final write leaves a truncated last line. Skipping
        unparseable lines is the whole point of the format: the run loses the one
        clip it was recording, not its history. The count is reported on
        self.skipped_lines (and self.torn_tail, true when the damage is confined
        to the final line, which is the benign case).
        """
        text = self._read_text()
        entries = []
        total = skipped = truncated = 0
        last_bad = -1
        lines = text.splitlines()
        for i, raw in enumerate(lines):
            if not raw.strip():
                continue
            total += 1
            bad = False
            try:
                obj = json.loads(raw)
            except ValueError:
                bad = True
            else:
                bad = not isinstance(obj, dict) or not obj.get("clip_id")
            if bad:
                skipped += 1
                last_bad = i
                # A record cut short by a kill still opens like one of ours and
                # simply stops; anything else is junk that got into the file some
                # other way, which is a different problem and worth saying so.
                if raw.startswith('{"clip_id":') and not raw.rstrip().endswith("}"):
                    truncated += 1
                continue
            entries.append(obj)
        self.total_lines = total
        self.skipped_lines = skipped
        self.truncated_lines = truncated
        #: True while the damage is still the very last line, i.e. nothing has
        #: been appended since the kill. _append() heals the tear on the next
        #: write, after which the same damaged line is simply mid-file.
        self.torn_tail = bool(skipped == 1 and last_bad == len(lines) - 1)
        if skipped and skipped != self._warned_skipped:
            self._warned_skipped = skipped
            junk = skipped - truncated
            print("[capture] manifest: %d of %d line(s) unreadable in %s "
                  "(%d truncated by a kill, %d unrecognised)%s"
                  % (skipped, total, self.path, truncated, junk,
                     "  ⚠ unrecognised lines are not a normal crash" if junk else ""))
        return entries

    def state(self):
        """Current state per clip_id, folded over the log."""
        return _fold(self.load())

    def known_clip_ids(self):
        """Every clip_id the log has ever mentioned, including missing ones.

        A missing clip is still known: re-adopting its directory if it came back
        would file it twice.
        """
        return set(self.state().keys())

    def kept_count(self):
        """Clips currently believed good and present on disk."""
        n = 0
        for e in self.state().values():
            if e.get("kept") and not e.get("missing"):
                n += 1
        return n

    # -- aggregate -----------------------------------------------------
    def summary(self):
        clips = list(self.state().values())
        kept, rejected, missing, pruned, adopted = [], [], [], 0, 0
        for c in clips:
            if c.get("adopted") or c.get("event") == "adopt":
                adopted += 1
            if c.get("pruned"):
                pruned += 1
            if c.get("missing"):
                missing.append(c)
            elif c.get("kept"):
                kept.append(c)
            else:
                rejected.append(c)

        by_label = collections.Counter()
        by_region = collections.Counter()
        by_scenario = collections.Counter()
        by_label_all = collections.Counter()
        by_region_all = collections.Counter()
        reasons = collections.Counter()
        seconds = 0.0
        n_bytes = 0
        frames = 0
        for c in clips:
            by_label_all[c.get("label") or "unlabelled"] += 1
            by_region_all[c.get("region") or "unknown"] += 1
        for c in kept:
            by_label[c.get("label") or "unlabelled"] += 1
            by_region[c.get("region") or "unknown"] += 1
            by_scenario[c.get("scenario") or "unknown"] += 1
            seconds += _as_float(c.get("duration_s"))
            n_bytes += _as_int(c.get("bytes"))
            frames += _as_int(c.get("frames"))
        for c in rejected + missing:
            for r in (c.get("reject_reasons") or []):
                reasons[_short_reason(r)] += 1

        # Counterfactual bookkeeping. `scenes` counts every scene the log has
        # heard of; the complete/incomplete split needs the PLAN, which only
        # scenes.jsonl has -- a scene whose first variation is still capturing has
        # one entry and no way to know from the log alone that three more are due.
        scene_ids = set(c.get("scene_id") for c in clips if c.get("scene_id"))
        planned = _load_scenes(self.scenes_path)
        progress = _scene_progress(planned, clips) if planned is not None else None

        stamps = sorted(c["created_utc"] for c in clips
                        if isinstance(c.get("created_utc"), str) and c["created_utc"])
        total = len(clips)
        return collections.OrderedDict([
            ("output_dir", self.output_dir),
            ("written_utc", utc_now_iso()),
            ("clips_total", total),
            ("kept", len(kept)),
            ("rejected", len(rejected)),
            ("missing", len(missing)),
            ("pruned", pruned),
            ("adopted", adopted),
            ("kept_pct", round(100.0 * len(kept) / total, 1) if total else 0.0),
            ("delivered_seconds", round(seconds, 2)),
            ("delivered_hms", _human_hms(seconds)),
            ("delivered_frames", frames),
            ("bytes", n_bytes),
            ("bytes_human", _human_bytes(n_bytes)),
            ("first_clip_utc", stamps[0] if stamps else None),
            ("last_clip_utc", stamps[-1] if stamps else None),
            ("by_label", collections.OrderedDict(by_label.most_common())),
            ("by_region", collections.OrderedDict(by_region.most_common())),
            ("by_scenario", collections.OrderedDict(by_scenario.most_common())),
            ("scenes", len(scene_ids)),
            ("scenes_complete", len(progress["complete"]) if progress else None),
            ("scenes_incomplete", len(progress["incomplete"]) if progress else None),
            ("scenes_all_kept", len(progress["all_kept"]) if progress else None),
            ("variations", _by_variation(clips)),
            ("by_label_all_clips", collections.OrderedDict(by_label_all.most_common())),
            ("by_region_all_clips", collections.OrderedDict(by_region_all.most_common())),
            ("reject_reasons", collections.OrderedDict(reasons.most_common())),
            ("metrics", _aggregate_metrics(kept)),
            ("log", collections.OrderedDict([
                ("path", self.path),
                ("lines", self.total_lines),
                ("skipped_lines", self.skipped_lines),
                ("truncated_lines", self.truncated_lines),
                ("torn_tail", self.torn_tail),
            ])),
        ])

    def write_summary(self):
        """Rewrite <output_dir>/summary.json atomically.

        ⚠ Temp file in the SAME directory, then os.replace: this is rewritten
        after every clip and something (a status script, the user's editor, a
        peer session) is routinely reading it. A plain open("w") exposes a
        truncated file for the length of the write, and a temp file on another
        filesystem makes the rename non-atomic. Returns the summary it wrote.
        """
        s = self.summary()
        tmp = self.summary_path + ".tmp"
        if not os.path.isdir(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, self.summary_path)
        return s

    # -- restart safety ------------------------------------------------
    def reconcile(self, force=False):
        """Make the log agree with the disk again. Call at startup, before capture.

        ★ This is what makes a restart safe. The two ways the log and the disk
        drift apart are a clip that was finalized when the process died (on disk,
        never recorded) and a clip whose directory was deleted behind the log's
        back (recorded, not on disk). Both are repaired by APPENDING, never by
        editing: an adopted clip gets a normal entry built from its own meta.json,
        a vanished one gets a "missing" line that keeps its history intact.

        Touches nothing on disk except the log itself -- pruning is the runner's
        decision, not the bookkeeper's.

        A counterfactual clip directory ("<scene_id>-<variation>") is adopted
        like any other: the id is whatever the directory is called, and its
        scene_id / variation are read out of its meta.json (see _scan_clip_dir).

        Refuses to mark a wholesale disappearance (see MISSING_SANITY_*) unless
        force=True, and says so in the returned dict instead.
        """
        state = _fold(self.load())
        adopted, incomplete, missing = [], [], []

        on_disk = []
        if os.path.isdir(self.clips_dir):
            for name in sorted(os.listdir(self.clips_dir)):
                if os.path.isdir(os.path.join(self.clips_dir, name)):
                    on_disk.append(name)

        for clip_id in on_disk:
            if clip_id in state:
                continue
            entry, ok = self._scan_clip_dir(clip_id)
            self.record(entry)
            if ok:
                adopted.append(clip_id)
            else:
                incomplete.append(clip_id)

        # Collect first, decide second: whether one clip vanished or the whole
        # volume did is only answerable once the count is known.
        n_live = 0
        suspect = []
        for clip_id, e in state.items():
            if e.get("missing"):
                continue                      # already corrected; stay idempotent
            # ⚠ Only kept clips are expected to still be on disk. A rejected clip
            # is pruned on purpose, so checking those would append a "missing"
            # line for every rejection on every single restart and bury the real
            # signal under its own noise.
            if not e.get("kept") or e.get("pruned"):
                continue
            n_live += 1
            clip_dir = os.path.join(self.clips_dir, clip_id)
            gone = not os.path.isdir(clip_dir)
            if not gone:
                files = [p for p in (e.get("files") or []) if p]
                if files and not os.path.exists(os.path.join(self.output_dir, files[0])):
                    gone = True               # directory survived, payload did not
            if gone:
                suspect.append(clip_id)

        refused = None
        if suspect and not force:
            limit = max(MISSING_SANITY_FLOOR, int(MISSING_SANITY_FRACTION * n_live))
            if not os.path.isdir(self.clips_dir):
                refused = ("clips/ is not present at all under %s -- the volume is "
                           "almost certainly not mounted" % self.output_dir)
            elif len(suspect) > limit:
                refused = ("%d of %d live clips look gone (limit %d) -- too many to "
                           "be normal attrition" % (len(suspect), n_live, limit))
        if refused:
            print("[capture] manifest: NOT marking %d clip(s) missing: %s"
                  % (len(suspect), refused))
        else:
            for clip_id in suspect:
                self.record({"clip_id": clip_id, "event": "missing", "missing": True,
                             "kept": False, "detected_utc": utc_now_iso(),
                             "note": "clip directory or payload no longer on disk"})
                missing.append(clip_id)

        return collections.OrderedDict([
            ("clips_on_disk", len(on_disk)),
            ("known_before", len(state)),
            ("adopted", adopted),
            ("adopted_incomplete", incomplete),
            ("missing", missing),
            ("missing_suspected", len(suspect)),
            ("refused", refused),
            ("appended", len(adopted) + len(incomplete) + len(missing)),
            ("skipped_log_lines", self.skipped_lines),
        ])

    def _scan_clip_dir(self, clip_id):
        """Build an entry for a clip directory the log never heard about.

        Returns (entry, complete). An incomplete clip is one with no readable
        meta.json -- almost always the clip that was mid-capture when the process
        died. It is filed as rejected so nothing downstream treats a partial clip
        as data, and the runner can prune it.
        """
        clip_dir = os.path.join(self.clips_dir, clip_id)
        meta_path = os.path.join(clip_dir, "meta.json")
        rel_root = os.path.join(CLIPS_DIRNAME, clip_id)

        files, n_bytes = [], 0
        for name in KEPT_FILES:
            p = os.path.join(clip_dir, name)
            if os.path.exists(p):
                files.append(os.path.join(rel_root, name).replace(os.sep, "/"))
                try:
                    n_bytes += os.path.getsize(p)
                except OSError:
                    pass

        meta = None
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = None

        # Directory mtime rather than now(): re-running reconcile must not keep
        # moving an adopted clip's creation time forward.
        created = _iso_from_mtime(meta_path if os.path.exists(meta_path) else clip_dir)

        if meta is None:
            # ⚠ scene_id / variation stay None even when the directory is called
            # "<scene_id>-<variation>". Splitting the name would be guessing (the
            # variation name may contain a hyphen), and a guessed scene_id would
            # let a half-captured directory count toward a scene's completeness.
            return ({"clip_id": clip_id, "event": "adopt", "adopted": True,
                     "kept": False, "label": None, "scenario": None, "region": None,
                     "duration_s": 0.0, "frames": 0, "bytes": n_bytes,
                     "created_utc": created, "files": files,
                     "reject_reasons": ["adopted from disk with no readable meta.json"],
                     "note": "likely interrupted mid-capture",
                     "scene_id": None, "variation": None}, False)

        # scene_id and variation come out of meta["scene"] inside entry_from_meta;
        # only the clip_id is taken from the directory, because that is the path
        # the manifest addresses.
        entry = entry_from_meta(meta, created_utc=created, files=files, n_bytes=n_bytes)
        entry["clip_id"] = clip_id            # the directory name is authoritative
        entry["event"] = "adopt"
        entry["adopted"] = True
        # ⚠ meta.json says the clip passed its gates; the video is what makes it
        # deliverable. writer.encode_video returns None on a frame/pose mismatch,
        # and then clip.mp4 was never written -- adopting that as kept would put a
        # clip with no video into the delivered count.
        if entry.get("kept") and not os.path.exists(os.path.join(clip_dir, "clip.mp4")):
            entry["kept"] = False
            entry["reject_reasons"] = list(entry.get("reject_reasons") or []) + [
                "adopted from disk without clip.mp4"]
        if os.path.isdir(os.path.join(clip_dir, "frames")):
            entry["has_frames_dir"] = True
        return entry, True


def _load_scenes(path):
    """scene_id -> Scene dict from scenes.jsonl, or None when the file is absent.

    None and an empty dict mean different things to the summary: no file is a
    run without variations (the completeness fields are reported null), an empty
    or all-torn file is a run that planned nothing yet.
    """
    if not os.path.exists(path):
        return None
    scenes = collections.OrderedDict()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return scenes
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict) or not obj.get("scene_id"):
            continue
        sid = str(obj["scene_id"])
        cur = scenes.get(sid)
        if cur is None:
            scenes[sid] = dict(obj)
        else:
            cur.update(obj)
    return scenes


def _variation_names(scene):
    """The planned variation names of a Scene line, or None when it only says
    how many. Accepts bare names or Variation dicts, since either is a natural
    thing for the runner to dump."""
    names = scene.get("variations")
    if not isinstance(names, list):
        return None
    out = []
    for v in names:
        if isinstance(v, dict):
            v = v.get("name")
        if v:
            out.append(str(v))
    return out or None


def _scene_progress(scenes, clips):
    """Sort the planned scenes into complete / incomplete / all_kept id lists.

    complete: every variation has an entry, kept OR rejected. Complete means the
    run is DONE with the scene, not that the grid is on disk: a rejected
    variation was adjudicated and pruned, and re-capturing it would not
    reproduce the same draw of GTA's ambient traffic anyway.
    all_kept: every variation kept and still present -- the number of whole
    counterfactual grids the delivered dataset actually contains, which is the
    overlap the variations exist to provide.
    ⚠ A "missing" entry still counts as adjudicated (it was kept, then vanished)
    but not as kept, so a lost clip moves its scene out of all_kept only.
    """
    seen = collections.defaultdict(set)
    kept = collections.defaultdict(set)
    for c in clips:
        sid, var = c.get("scene_id"), c.get("variation")
        if not sid or not var:
            continue
        seen[sid].add(var)
        if c.get("kept") and not c.get("missing"):
            kept[sid].add(var)

    complete, incomplete, all_kept = [], [], []
    for sid, scene in scenes.items():
        names = _variation_names(scene)
        if names:
            # ⚠ Belt and braces against a writer that lists only the variations
            # that ran: a plan shorter than n_variations cannot be "complete".
            want = scene.get("n_variations")
            enough = (not isinstance(want, int)) or (len(names) >= want)
            done = bool(names) and enough and all(nm in seen[sid] for nm in names)
            full = all(nm in kept[sid] for nm in names)
        else:
            n = _as_int(scene.get("n_variations"))
            # A plan line that names no count is unjudgeable; it stays incomplete
            # rather than being counted done by a zero.
            done = n > 0 and len(seen[sid]) >= n
            full = n > 0 and len(kept[sid]) >= n
        (complete if done else incomplete).append(sid)
        if done and full:
            all_kept.append(sid)
    return {"complete": complete, "incomplete": incomplete, "all_kept": all_kept}


def _by_variation(clips):
    """Per-variation outcome breakdown, in first-seen (capture) order.

    ★ This table is what the variations are for: the same scenes, so any
    difference between rows is the behaviour dial and not the location or the
    incident. `by_label` is over kept clips and `by_label_all_clips` over every
    entry, mirroring the top-level pair so both levels of the summary mean the
    same thing -- a variation whose ego wrecks itself out of the quality gates
    shows up in the second and not the first.
    """
    out = collections.OrderedDict()
    for c in clips:
        name = c.get("variation")
        if not name or not c.get("scene_id"):
            continue
        v = out.get(name)
        if v is None:
            v = out[name] = {"clips": 0, "kept": 0,
                             "by_label": collections.Counter(),
                             "by_label_all_clips": collections.Counter()}
        label = c.get("label") or "unlabelled"
        v["clips"] += 1
        v["by_label_all_clips"][label] += 1
        if c.get("kept") and not c.get("missing"):
            v["kept"] += 1
            v["by_label"][label] += 1
    for v in out.values():
        v["by_label"] = collections.OrderedDict(v["by_label"].most_common())
        v["by_label_all_clips"] = collections.OrderedDict(
            v["by_label_all_clips"].most_common())
    return out


def _aggregate_metrics(clips):
    """min/median/p90/max per numeric metric, true-rate per boolean one."""
    numeric = collections.defaultdict(list)
    flag_true = collections.Counter()
    flag_seen = collections.Counter()
    for c in clips:
        m = c.get("metrics")
        if not isinstance(m, dict):
            continue
        for k, v in m.items():
            # ⚠ bool before int: bool IS an int in Python, so the obvious order
            # turns "collided" into a numeric series of 0.0 and 1.0 and reports
            # its median instead of the rate anyone wanted.
            if isinstance(v, bool):
                flag_seen[k] += 1
                if v:
                    flag_true[k] += 1
            elif isinstance(v, (int, float)):
                numeric[k].append(float(v))

    out = collections.OrderedDict()
    for k in sorted(numeric):
        vals = sorted(numeric[k])
        out[k] = collections.OrderedDict([
            ("n", len(vals)),
            ("min", round(vals[0], 3)),
            ("median", round(_pct(vals, 50), 3)),
            ("p90", round(_pct(vals, 90), 3)),
            ("max", round(vals[-1], 3)),
            ("mean", round(sum(vals) / len(vals), 3)),
        ])
    for k in sorted(flag_seen):
        out[k] = collections.OrderedDict([
            ("n", flag_seen[k]),
            ("true", flag_true[k]),
            ("true_pct", round(100.0 * flag_true[k] / flag_seen[k], 1)),
        ])
    return out


def _oneline(s, limit=40):
    return " ".join(str(s).split())[:limit]


def format_summary(s):
    """One screen of plain text from a summary() dict."""
    lines = []
    log = s.get("log") or {}
    damage = ""
    if log.get("skipped_lines"):
        damage = "  ⚠ %d unreadable (%d truncated by a kill)" % (
            log["skipped_lines"], log.get("truncated_lines", 0))
    lines.append("output     : %s" % s["output_dir"])
    lines.append("manifest   : %d lines%s" % (log.get("lines", 0), damage))
    lines.append("clips      : %d recorded, %d kept (%.0f%%), %d rejected, %d missing"
                 % (s["clips_total"], s["kept"], s["kept_pct"], s["rejected"],
                    s["missing"]))
    lines.append("delivered  : %s of video, %d frames, %s"
                 % (s["delivered_hms"], s["delivered_frames"], s["bytes_human"]))
    if s.get("first_clip_utc"):
        lines.append("span       : %s .. %s" % (s["first_clip_utc"], s["last_clip_utc"]))
    if s.get("adopted") or s.get("pruned"):
        lines.append("bookkeeping: %d adopted from disk, %d pruned after rejection"
                     % (s.get("adopted", 0), s.get("pruned", 0)))

    def _row(title, counter, limit=8):
        if not counter:
            return
        items = list(counter.items())[:limit]
        tail = " (+%d more)" % (len(counter) - limit) if len(counter) > limit else ""
        # Labels and reasons come from clip metadata, so a stray newline in one of
        # them would break the one-line-per-row layout this screen is read as.
        lines.append("%-11s: %s%s"
                     % (title, ", ".join("%s %d" % (_oneline(k), v) for k, v in items),
                        tail))

    _row("outcomes", s.get("by_label"))
    _row("regions", s.get("by_region"))
    _row("scenarios", s.get("by_scenario"))
    _row("rejects", s.get("reject_reasons"), limit=6)

    # Counterfactual block: silent for a run without variations, so the screen a
    # single-clip run prints is unchanged.
    n_scenes = _as_int(s.get("scenes"))
    if n_scenes or s.get("scenes_complete") is not None:
        text = "%d distinct" % n_scenes
        if s.get("scenes_complete") is not None:
            text += (", %d complete, %d incomplete (planned in scenes.jsonl), "
                     "%d with every variation kept"
                     % (_as_int(s.get("scenes_complete")),
                        _as_int(s.get("scenes_incomplete")),
                        _as_int(s.get("scenes_all_kept"))))
        lines.append("scenes     : %s" % text)
    variations = s.get("variations") or {}
    if variations:
        width = max([4] + [len(_oneline(k, 24)) for k in variations])
        lines.append("variations :")
        lines.append("    %-*s %5s %5s  %s" % (width, "name", "clips", "kept", "kept outcomes"))
        for name, v in variations.items():
            labels = list((v.get("by_label") or {}).items())[:6]
            outcomes = ", ".join("%s %d" % (_oneline(k), n) for k, n in labels)
            lines.append("    %-*s %5d %5d  %s"
                         % (width, _oneline(name, 24), _as_int(v.get("clips")),
                            _as_int(v.get("kept")), outcomes or "-"))

    metrics = s.get("metrics") or {}
    if metrics:
        lines.append("metrics    :")
        for k in metrics:
            m = metrics[k]
            if "true_pct" in m:
                lines.append("    %-24s %d/%d clips (%.0f%%)"
                             % (k, m["true"], m["n"], m["true_pct"]))
            else:
                lines.append("    %-24s med %-9g p90 %-9g max %-9g"
                             % (k, m["median"], m["p90"], m["max"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Print the capture manifest summary for an output directory.")
    ap.add_argument("output_dir")
    ap.add_argument("--json", action="store_true", help="print summary.json instead")
    ap.add_argument("--reconcile", action="store_true",
                    help="check the log against the clips on disk first "
                         "(appends corrections; do not use while capturing)")
    ap.add_argument("--force", action="store_true",
                    help="with --reconcile, mark clips missing even when it looks "
                         "like the whole volume vanished")
    ap.add_argument("--write", action="store_true",
                    help="also rewrite <output_dir>/summary.json")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.output_dir):
        print("[capture] no such output directory: %s" % args.output_dir)
        return 2
    m = Manifest(args.output_dir)
    if not os.path.exists(m.path):
        print("[capture] no manifest at %s" % m.path)
        return 1
    if args.reconcile:
        r = m.reconcile(force=args.force)
        print("[capture] reconcile: %d on disk, adopted %d (%d incomplete), "
              "marked %d missing"
              % (r["clips_on_disk"], len(r["adopted"]), len(r["adopted_incomplete"]),
                 len(r["missing"])))
        if r["refused"]:
            print("[capture] ⚠ %d clip(s) look gone but were NOT recorded missing: %s"
                  % (r["missing_suspected"], r["refused"]))
            print("[capture]   fix the mount, or re-run with --force if the loss "
                  "is real")
    s = m.write_summary() if args.write else m.summary()
    print(json.dumps(s, indent=2) if args.json else format_summary(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
