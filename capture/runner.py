"""Supervised capture loop: keep the game alive, keep the clips coming.

    python3 run_capture.py --config capture.json

One CHUNK is one game lifetime's worth of clips. The loop exists because the game
dies: an overnight run lost 7 of 18 chunks to a silent crash -- no ScriptHookV
exception, no resource signature (RSS flat, 28 GB free, no handle growth). Until
that is understood the way to capture continuously is to make the death cheap
rather than fatal: run a bounded chunk, notice the death, relaunch, carry on.

Restarting the script continues a run instead of starting a new one. Everything
that carries state lives in output_dir -- manifest.jsonl, coverage_ledger.json,
outcome_ledger.json -- so chunk N+1 keeps balancing regions and outcomes against
everything chunk N already produced, including across restarts.

Finalizing (encode, prune, record) happens on the Finalizer's background threads
while the NEXT chunk is already capturing. The loop deliberately never drains
between chunks; it drains once, at exit.

With `variations` set, one proposed location becomes one SCENE captured N times --
same place, conditions, ego vehicle and staged incident, different ego/actor
behaviour per variation. The runner's part is small: it hands the generator the
plan (settings.variation_configs(), written into generator_config.json under
"variation_plan") and sizes every chunk to whole scenes, so a game death between
chunks never leaves a scene half-captured.
"""

import json
import os
import random
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .finalize import Finalizer
from .game import Game, clip_library_dir
from .manifest import Manifest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_VPILOT = os.path.join(_REPO, "VPilot")
_GENERATOR = os.path.join(_VPILOT, "longtail", "run_generator.py")

#: How long to wait for the game to come up before calling the launch failed.
_LAUNCH_DEADLINE_S = 360
#: Consecutive failed launches before giving up. Three is a broken install or a
#: locked Steam client, not a transient -- spinning on it burns the time budget.
_MAX_LAUNCH_FAILS = 3
#: Consecutive chunks that produce no clip at all before giving up. A game that
#: comes up healthy and still yields nothing is a fault the relaunch cannot fix.
_MAX_EMPTY_CHUNKS = 3
#: Poll interval while a chunk runs. Short so a Ctrl-C is acted on promptly.
_POLL_S = 1.0
#: How often to sweep for finished clips while the chunk is still running.
_SCAN_S = 20.0
#: A clip's meta.json must be this old before it is handed over, so a file caught
#: mid-write is never parsed. Clips take ~100 s each; this costs nothing.
_META_SETTLE_S = 3.0
#: After a stop request, how long the generator gets to finish its current clip.
_STOP_GRACE_S = 180.0
#: Between SIGTERM and SIGKILL when a chunk has to be ended by force.
_KILL_AFTER_S = 20.0

_GPU_LOCK_CANDIDATES = (
    os.path.join(_REPO, "tools", "gpu_lock.sh"),
    os.path.join(os.path.dirname(os.path.dirname(_REPO)), "tools", "gpu_lock.sh"),
)

_STOP = False
_STOP_REQUESTS = 0


def _handle_stop(signum, frame):
    global _STOP, _STOP_REQUESTS
    _STOP = True
    _STOP_REQUESTS += 1
    if _STOP_REQUESTS == 1:
        sys.stdout.write("\n[capture] stop requested; finishing the current clip, "
                         "then draining. Press Ctrl-C again to cut it short.\n")
    else:
        sys.stdout.write("\n[capture] second stop request; ending the chunk now.\n")
    sys.stdout.flush()


# ----------------------------------------------------------------------------
# helpers


def _normalize(settings):
    """Absolute output path, so every component agrees on where the run lives."""
    settings.output_dir = os.path.abspath(os.path.expanduser(settings.output_dir))
    return settings


def _fmt_hms(seconds):
    seconds = int(max(0, seconds))
    return "%d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def _fmt_mapping(d):
    """Render an unspecified dict compactly: collections become their size."""
    if not isinstance(d, dict) or not d:
        return "(nothing)"
    parts = []
    for k in sorted(d, key=lambda x: str(x)):
        v = d[k]
        if isinstance(v, (list, tuple, set, dict)):
            v = len(v)
        parts.append("%s=%s" % (k, v))
    return ", ".join(parts)


#: summary() keys that describe counterfactual scenes. Printed as their own block
#: by _print_scene_report, and skipped by the generic printer: it would render the
#: per-variation dicts as their sizes, and a single-clip run would grow a
#: "scenes 0" line it never had.
_SCENE_KEYS = frozenset(("scenes", "scenes_complete", "scenes_incomplete",
                         "scenes_all_kept", "variations"))


def _print_summary(summary, out_dir):
    print("[capture] summary  (%s)" % os.path.join(out_dir, "summary.json"))
    if not isinstance(summary, dict) or not summary:
        print("             (empty -- nothing has been captured here yet)")
        return
    for k in sorted(summary, key=lambda x: str(x)):
        if k in _SCENE_KEYS:
            continue
        v = summary[k]
        if isinstance(v, dict):
            if not v:
                continue
            v = _fmt_mapping(v)
        elif "bytes" in str(k) and isinstance(v, (int, float)):
            v = "%.2f GB" % (float(v) / 1e9)
        elif "seconds" in str(k) and isinstance(v, (int, float)):
            v = "%.0f s (%s)" % (v, _fmt_hms(v))
        print("             %-18s %s" % (k, v))


def _sleep_interruptible(seconds):
    """Sleep, but wake up as soon as a stop is requested."""
    end = time.time() + seconds
    while time.time() < end and not _STOP:
        time.sleep(min(0.5, max(0.0, end - time.time())))


def _generator_config_path(settings):
    # Distinct from capture_config.json, which run_generator.py writes itself as a
    # record of the config it actually resolved. This one is the input.
    return os.path.join(settings.output_dir, "generator_config.json")


#: Key under which the variation plan rides along inside generator_config.json.
#: The generator's CaptureConfig.load() keeps it as a plain attribute; the value
#: is a list of [Variation, generator_config_dict] pairs.
_PLAN_KEY = "variation_plan"


def _variation_plan(settings):
    """[(Variation, generator_config_dict), ...] -- empty when variations are off.

    Settings without the feature at all (an older capture/ checkout, or a
    settings object built by hand) mean off, the same as `variations: []`.
    """
    configs = getattr(settings, "variation_configs", None)
    if configs is None:
        return []
    return [(dict(var), dict(gen)) for var, gen in configs()]


def _variation_presets():
    """Name -> list of Variation dicts, from capture.settings."""
    from . import settings as settings_mod
    presets = getattr(settings_mod.CaptureSettings, "PRESETS", None)
    if presets is None:
        presets = getattr(settings_mod, "PRESETS", None)
    return dict(presets or {})


def _preset_name(settings):
    """The preset name a settings' variation list matches, or None."""
    current = getattr(settings, "variations", None) or []
    for name, plan in _variation_presets().items():
        if list(plan) == list(current):
            return name
    return None


def override_variations(settings, spec):
    """Apply a command-line `--variations` value. Returns an error string or None.

    `spec` is a preset name or "off". The preset is expanded here rather than
    stored as a string because CaptureSettings.load() has already run -- that is
    where a string would normally have been expanded.
    """
    spec = str(spec).strip()
    if spec.lower() in ("off", "none", "0", ""):
        settings.variations = []
        return None
    presets = _variation_presets()
    if spec not in presets:
        return ("unknown variations preset %r; use one of: %s, or \"off\""
                % (spec, ", ".join(sorted(presets)) or "(none defined)"))
    # Copies, not the shared preset table: validate() and callers may adjust.
    settings.variations = [dict(v) for v in presets[spec]]
    return None


def _resolve_clip_library(settings):
    """Fill settings.clip_library_dir from the registry when .clip recording is on.

    Done here, on the runner's side of the fence, because the generator has no
    business reading the Windows registry -- and because every variation config
    is derived from the settings object, so filling the field once covers them.
    """
    if getattr(settings, "record_clip", False) and not getattr(settings, "clip_library_dir", ""):
        settings.clip_library_dir = clip_library_dir()
    return settings.clip_library_dir


def _write_generator_config(settings):
    path = _generator_config_path(settings)
    _resolve_clip_library(settings)
    cfg = settings.to_generator_config()
    plan = _variation_plan(settings)
    if plan:
        cfg[_PLAN_KEY] = [[var, gen] for var, gen in plan]
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    return path, cfg


def _check_generator_config(cfg):
    """Would longtail's CaptureConfig accept this dict? Returns a human string.

    CaptureConfig.load() does CaptureConfig(**json), so one unexpected key is a
    TypeError that kills every chunk identically. Cheaper to find out here than
    after a 6-minute game launch.

    ★ Goes through load() on a scratch copy, not the constructor: load() is the
    call the generator actually makes, and the variation plan rides along in the
    same file under a key the constructor does not know. Whether load() tolerates
    that key is exactly the question. Each variation's own config is then checked
    as a plain constructor call, since that is how the generator will use it.
    """
    cfg_py = os.path.join(_VPILOT, "longtail", "config.py")
    if not os.path.isfile(cfg_py):
        return "cannot verify: %s not found" % cfg_py
    # ⚠ longtail's modules are flat and generically named (config, writer,
    # outcomes...). Append rather than insert so nothing here shadows a real
    # import for the rest of the process.
    d = os.path.dirname(cfg_py)
    if d not in sys.path:
        sys.path.append(d)
    plan = cfg.get(_PLAN_KEY) or []
    try:
        from config import CaptureConfig
        fd, tmp = tempfile.mkstemp(prefix="generator_config.", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cfg, f)
            loaded = CaptureConfig.load(tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        for _var, gen in plan:
            CaptureConfig(**gen)
    except TypeError as exc:
        return "REJECTED by longtail CaptureConfig: %s" % exc
    except Exception as exc:                       # pragma: no cover - env specific
        return "cannot verify: %r" % (exc,)
    if plan and not getattr(loaded, _PLAN_KEY, None):
        # ⚠ Worse than a TypeError: the run would look healthy and capture single
        # clips while the user believes every scene is being captured N times.
        return ("REJECTED: longtail CaptureConfig.load() dropped %r -- the generator "
                "would capture single clips, not %d variations per scene"
                % (_PLAN_KEY, len(plan)))
    n_fields = len(cfg) - (1 if _PLAN_KEY in cfg else 0)
    return ("accepted by longtail CaptureConfig (%d field(s)%s)"
            % (n_fields, ", %d variation config(s)" % len(plan) if plan else ""))


def _cell(v):
    """One table cell: JSON spelling for everything but strings, so True is
    `true` (as the generator will read it) and 0.95 is not 0.95000000000000001."""
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True)


def _print_variation_plan(plan, settings):
    """The plan as a table: one column per variation, one row per knob that
    differs between them. Knobs identical across the plan are scene knobs (or
    plain settings) and are not what a user is checking here."""
    names = [str(var.get("name")) for var, _ in plan]
    preset = _preset_name(settings)
    print("\n[capture] variations: %d per scene%s -- each proposed location is captured "
          "%d times" % (len(plan), " (preset %r)" % preset if preset else "", len(plan)))
    print("    held constant per scene: location, region, weather, time, ego vehicle, "
          "ambient density, seeded population counts, and the staged incident")
    print("    ⚠ ambient traffic and the random ped/vehicle models are the game's own "
          "draws; the variations are not frame-identical replays")

    keys = set()
    for _, gen in plan:
        keys.update(gen)
    differing = [k for k in sorted(keys)
                 if len(set(_cell(gen.get(k)) for _, gen in plan)) > 1]

    def _follows(dial, knob):
        """Does the knob take one value per distinct value of this dial?"""
        seen = {}
        for var, gen in plan:
            d = _cell(var.get(dial))
            if seen.setdefault(d, _cell(gen.get(knob))) != _cell(gen.get(knob)):
                return False
        return True

    # ★ Read off the plan itself which dial each knob follows, rather than
    # trusting a table of who-owns-what: a knob that ends up in the wrong group
    # is precisely the mistake this printout exists to catch.
    def _group(knob):
        ego, actor = _follows("ego_chaos", knob), _follows("actor_chaos", knob)
        if ego and not actor:
            return 0, "ego"
        if actor and not ego:
            return 1, "actor"
        return 2, ""
    differing.sort(key=lambda k: (_group(k)[0], k))

    rows = [("", "ego_chaos", [_cell(var.get("ego_chaos")) for var, _ in plan]),
            ("", "actor_chaos", [_cell(var.get("actor_chaos")) for var, _ in plan])]
    rows += [(_group(k)[1], k, [_cell(gen.get(k)) for _, gen in plan]) for k in differing]

    widths = [max(len(n), max(len(r[2][i]) for r in rows)) for i, n in enumerate(names)]
    group_w = max(len(r[0]) for r in rows)
    label_w = max(len("knob"), max(len(r[1]) for r in rows))
    lines = [(" " * group_w, "knob", names)] + rows
    for i, (group, label, cells) in enumerate(lines):
        print(("    %-*s %-*s  %s" % (group_w, group, label_w, label,
                                      "  ".join(c.ljust(w) for c, w in zip(cells, widths))))
              .rstrip())
        if i == 0:
            print("    " + "-" * (group_w + 1 + label_w + 2 + sum(widths) + 2 * (len(widths) - 1)))
    if not differing:
        print("    ⚠ no generator knob differs between these variations -- the clips "
              "would differ only by name")


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _print_scene_report(summary, expected_names=()):
    """The counterfactual block of a manifest summary(): one row per variation
    and the scene completeness line. Returns False (and prints nothing) when the
    summary carries no scene clip, so a single-clip run's screen is unchanged.

    Rows follow the CONFIGURED plan first: a variation that has never produced a
    clip is absent from the manifest, and its row of zeros is the news.
    """
    if not isinstance(summary, dict):
        return False
    variations = summary.get("variations") or {}
    n_scenes = _int(summary.get("scenes"))
    if not variations and not n_scenes:
        return False

    expected = [str(n) for n in expected_names if n is not None]
    names = expected + [n for n in variations if n not in expected]
    w = max([len("variation")] + [len(str(n)) for n in names])
    print("[capture] scenes    %d distinct in the manifest" % n_scenes)
    print("             %-*s  %6s  %6s  %5s  %s" % (w, "variation", "clips", "kept", "kept%",
                                                     "kept outcomes"))
    for name in names:
        v = variations.get(name) or {}
        clips, kept = _int(v.get("clips")), _int(v.get("kept"))
        labels = list((v.get("by_label") or {}).items())[:4]
        outcomes = ", ".join("%s %d" % (k, _int(n)) for k, n in labels)
        if not clips:
            outcomes = "never captured" if name in expected else "-"
        print("             %-*s  %6d  %6d  %4.0f%%  %s"
              % (w, name, clips, kept, 100.0 * kept / clips if clips else 0.0,
                 outcomes or "-"))
    # Completeness needs the PLAN (which variations each scene was due), which
    # lives in scenes.jsonl, not in the clip log: a scene whose first variation
    # is still capturing is one entry with no way to tell three more are due.
    if summary.get("scenes_complete") is None:
        print("             completeness unknown: no scenes.jsonl beside the manifest "
              "(the generator appends each scene's plan there)")
    else:
        print("             %d complete (every variation captured or rejected), "
              "%d incomplete, %d with every variation kept"
              % (_int(summary.get("scenes_complete")),
                 _int(summary.get("scenes_incomplete")),
                 _int(summary.get("scenes_all_kept"))))
    return True


def _gpu_lock_script():
    """The shared advisory GPU lock, if this machine has one.

    ★ This box runs several sessions against one 16 GB card, and the card SPILLS
    instead of OOMing -- two concurrent jobs measured 2.7-3.0x slower each for
    0.70x aggregate throughput, with VRAM 80% free. Contention is invisible, so
    it has to be scheduled. Absent (a normal single-user machine) the chunk just
    runs directly; set CAPTURE_GPU_LOCK=0 to opt out, or to a path to override.
    """
    override = os.environ.get("CAPTURE_GPU_LOCK")
    if override is not None:
        if override.strip().lower() in ("", "0", "no", "off", "false"):
            return None
        return override if os.path.isfile(override) else None
    for cand in _GPU_LOCK_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return None


def _chunk_command(settings, cfg_path, want, seed):
    return [sys.executable, "-u", _GENERATOR,
            "--config", cfg_path,
            "--out", settings.output_dir,
            "--host", str(settings.host),
            "--port", str(settings.port),
            "--max-clips", str(want),
            "--seed", str(seed)]


def _wrap_with_gpu_lock(cmd, lock_sh, desc, est_s):
    inner = " ".join(shlex.quote(a) for a in cmd)
    # ⚠ No `exec` here: exec replaces the shell and the EXIT trap never runs, so
    # the lock would outlive the job. ⚠ The INT/TERM trap must exit, or bash
    # treats the signal as handled and carries on.
    script = "\n".join([
        "set -u",
        ". %s" % shlex.quote(lock_sh),
        "export GPU_OWNER=%s" % shlex.quote(os.environ.get("GPU_OWNER", "gtav-capture")),
        "trap 'gpu_release signal' EXIT",
        "trap 'gpu_release signal; exit 130' INT TERM",
        "gpu_acquire %s %d" % (shlex.quote(desc), int(est_s)),
        inner,
        "rc=$?",
        "gpu_release",
        "exit $rc",
    ])
    return ["bash", "-c", script]


def _chunk_env():
    env = dict(os.environ)
    # ⚠ run_generator.py runs `from longtail import writer` BEFORE it repairs
    # sys.path, so VPilot has to be importable already. It works interactively
    # only because this machine's PYTHONPATH happens to start with an empty entry
    # (i.e. cwd) and the supervisor cd'd into VPilot. Clear PYTHONPATH and the
    # generator dies on its twelfth line. Never inherit that piece of luck.
    prev = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _VPILOT + ((os.pathsep + prev) if prev else "")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _signal_group(proc, sig):
    """Signal the child's whole process group.

    ⚠ The child may be a bash wrapper around python (the GPU lock), so signalling
    proc.pid alone leaves the generator running and still holding the ZMQ PAIR
    slot -- which then blackholes the next chunk's connection. ⚠ And never reach
    for `pkill -f <pattern>` to clean this up: the pattern matches this
    supervisor's own command line as readily as the generator's.
    """
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except OSError:
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _tee(stream, log_path):
    """Generator output to both the console and output_dir/capture.log."""
    try:
        with open(log_path, "a") as log:
            for line in iter(stream.readline, ""):
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
                log.flush()
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# clip discovery


def _needs_finalizing(clip_dir, meta):
    """Was this clip written but never finalized (a previous run was killed)?"""
    if meta.get("keep"):
        return not os.path.isfile(os.path.join(clip_dir, "clip.mp4"))
    # ⚠ A rejected clip's whole DIRECTORY goes -- the manifest entry is the only
    # thing that survives it -- so any reject directory still on disk is unpruned,
    # frames or no frames. Testing for a frames/ dir instead missed the rejects
    # the generator had already stripped (video_only prunes those itself), which
    # then sat there as husks that no later run would ever clean up. keep_frames
    # is not a factor either: it governs kept clips, not rejected ones.
    return os.path.isdir(clip_dir)


def _collect_new_clips(settings, manifest, finalizer, state, settle_s=_META_SETTLE_S):
    """Hand every newly completed clip directory to the finalizer.

    ★ The generator writes the clips; this only notices them. A clip directory is
    complete when it contains meta.json -- writer.finalize() writes it last, after
    poses.jsonl is closed and every frame is on disk.

    ⚠ Submitting twice would encode twice and record twice, so a directory is
    handed over only if neither the manifest nor this run has seen it.
    """
    clips_dir = os.path.join(settings.output_dir, "clips")
    try:
        names = sorted(os.listdir(clips_dir))
    except OSError:
        return []

    known = manifest.known_clip_ids()
    now = time.time()
    found = []
    for cid in names:
        if cid in state["submitted"] or cid in known:
            continue
        clip_dir = os.path.join(clips_dir, cid)
        meta_path = os.path.join(clip_dir, "meta.json")
        if not os.path.isfile(meta_path):
            continue                       # still capturing
        try:
            if settle_s > 0 and (now - os.path.getmtime(meta_path)) < settle_s:
                continue                   # written this instant; catch it next sweep
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue                       # half-written or unreadable; retry later
        # ⚠ Only when the generator config leaves `video` on: run_generator.py then
        # writes meta.json TWICE for a kept clip -- once from writer.finalize(),
        # then again ~20 s later once its own encode has finished -- and deletes
        # the frames in between. A sweep landing in that window would hand the
        # directory over while the generator is still encoding it, so two encoders
        # would write the same clip.mp4 and race the rmtree. The video block only
        # exists after the generator is done. settle_s == 0 means the chunk has
        # exited, so nothing can be mid-encode and an unencoded clip is simply one
        # whose encode failed -- take it then.
        if (settle_s > 0 and state.get("gen_encodes")
                and meta.get("keep") and not meta.get("video")):
            continue
        state["submitted"].add(cid)
        if meta.get("keep"):
            state["keep_ids"].add(cid)
        finalizer.submit(clip_dir, meta)
        found.append((cid, bool(meta.get("keep"))))
    return found


def _resubmit_unfinished(settings, manifest, finalizer, state, adopted):
    """Finish clips a previous run captured but was killed before finalizing.

    Those are exactly the ids reconcile() had to adopt: the finalizer records a
    clip when it is done with it, so anything on disk that the log had never
    heard of was never encoded or pruned.
    """
    clips_dir = os.path.join(settings.output_dir, "clips")
    resubmitted = []
    for cid in sorted(adopted):
        clip_dir = os.path.join(clips_dir, cid)
        meta_path = os.path.join(clip_dir, "meta.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if not _needs_finalizing(clip_dir, meta):
            state["submitted"].add(cid)
            continue
        state["submitted"].add(cid)
        if meta.get("keep"):
            state["keep_ids"].add(cid)
        finalizer.submit(clip_dir, meta)
        resubmitted.append(cid)
    return resubmitted


def _kept_estimate(manifest, state):
    """Kept clips on record, plus the ones in flight the finalizer has not booked.

    Without the in-flight term the loop double-counts its own progress the wrong
    way: it would launch another whole game lifetime while the clips that already
    met the target are still encoding.
    """
    known = manifest.known_clip_ids()
    return manifest.kept_count() + len(state["keep_ids"] - known)


# ----------------------------------------------------------------------------
# one chunk


def _round_up(n, multiple):
    multiple = max(1, int(multiple))
    return ((int(n) + multiple - 1) // multiple) * multiple


def _lifetime_clips(settings, per_scene=1):
    """clips_per_lifetime, rounded UP to whole scenes.

    ★ A scene's variations run inside ONE game lifetime. The game dies roughly
    one chunk in three at scale, and the chunk boundary is the one place the run
    stops the generator on purpose -- a scene straddling it is a scene left
    incomplete by design. Up rather than down, so a lifetime is never shorter
    than one scene and a 12-clip setting with 5 variations does not silently
    become 10.
    """
    return _round_up(max(1, int(settings.clips_per_lifetime)), per_scene)


def _chunk_size(settings, kept, target, per_scene=1):
    per_scene = max(1, int(per_scene))
    per = _lifetime_clips(settings, per_scene)
    if not target:
        return per
    remaining = max(1, target - kept)
    # ⚠ Ask for ATTEMPTS, not keeps. The quality gates reject roughly a quarter of
    # clips, so sizing the last chunk to the exact shortfall reliably ends a clip
    # or two short and costs another whole game lifetime to make up.
    attempts = int(remaining * 1.4) + 1
    # Whole scenes again: the target counts clips, but a chunk that ends mid-scene
    # would leave the last scene of the run incomplete.
    return max(per_scene, min(per, _round_up(attempts, per_scene)))


def _fmt_chunk(want, per_scene):
    if per_scene > 1:
        return "%d clip(s) = %d scene(s) x %d" % (want, want // per_scene, per_scene)
    return "%d clip(s)" % want


def _chunk_budget_s(settings, want):
    """Wall-clock ceiling for a chunk.

    Measured ~100 s per 15 s clip end to end (relocate, warmup, ~450 frames of
    JPEG writing). This is a backstop for a wedged generator, not a schedule, so
    it is deliberately ~1.8x that, and scales with clip length to keep the ratio.
    """
    per_clip = 90.0 + 6.0 * (float(settings.clip_duration_s) + float(settings.discard_lead_s))
    return 120.0 + want * per_clip


def _run_chunk(settings, cfg_path, want, seed, manifest, finalizer, state, per_scene=1):
    """Run one game lifetime's worth of clips. Returns a result dict."""
    budget_s = _chunk_budget_s(settings, want)
    cmd = _chunk_command(settings, cfg_path, want, seed)
    lock_sh = _gpu_lock_script()
    if lock_sh:
        cmd = _wrap_with_gpu_lock(
            cmd, lock_sh, "GTA V capture: %s" % _fmt_chunk(want, per_scene), budget_s)

    log_path = os.path.join(settings.output_dir, "capture.log")
    t0 = time.time()
    # start_new_session puts the child in its own process group: the terminal's
    # Ctrl-C reaches only this supervisor, which then forwards it deliberately
    # (so the generator finishes the clip it is on instead of losing it), and one
    # killpg reaches the wrapper and the generator together.
    # ⚠ The flip side is that SIGKILLing this supervisor orphans the generator
    # rather than taking it with it: it keeps capturing to its --max-clips and
    # keeps the ZMQ PAIR slot. Stop a run with Ctrl-C, not kill -9; after a kill
    # -9, tools/kill_stale_clients.sh clears the orphan. Its clips are not lost
    # either way -- reconcile() adopts them and they are finalized on restart.
    proc = subprocess.Popen(cmd, cwd=_VPILOT, env=_chunk_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            universal_newlines=True, bufsize=1,
                            start_new_session=True)
    tee = threading.Thread(target=_tee, args=(proc.stdout, log_path))
    tee.daemon = True
    tee.start()

    hard_deadline = t0 + budget_s
    grace_until = None
    kill_reason = None
    kill_at = None
    signalled = False
    last_scan = time.time()

    while True:
        rc = proc.poll()
        if rc is not None:
            break
        now = time.time()

        if _STOP and not signalled:
            print("[capture] asking the generator to finish its clip and stop")
            _signal_group(proc, signal.SIGINT)
            signalled = True
            grace_until = now + _STOP_GRACE_S

        if kill_reason is None:
            if now >= hard_deadline:
                kill_reason = "chunk exceeded its %s budget" % _fmt_hms(budget_s)
            elif signalled and _STOP_REQUESTS >= 2:
                kill_reason = "second stop request"
            elif signalled and grace_until and now >= grace_until:
                kill_reason = ("generator did not stop within %s of being asked"
                               % _fmt_hms(_STOP_GRACE_S))
            if kill_reason:
                print("[capture] %s -- ending it" % kill_reason)
                _signal_group(proc, signal.SIGTERM)
                kill_at = now + _KILL_AFTER_S
        elif kill_at is not None and now >= kill_at:
            print("[capture] still alive after SIGTERM -- SIGKILL")
            _signal_group(proc, signal.SIGKILL)
            kill_at = None

        # ★ Sweep while the chunk is still running, so a clip finished five
        # minutes ago is already encoding rather than waiting for the chunk to
        # end. This is what keeps finalize off the capture's critical path.
        if now - last_scan >= _SCAN_S:
            last_scan = now
            _collect_new_clips(settings, manifest, finalizer, state)

        time.sleep(_POLL_S)

    tee.join(timeout=10.0)
    # The generator has exited, so nothing is being written any more: no settle.
    found = _collect_new_clips(settings, manifest, finalizer, state, settle_s=0.0)
    return {"rc": proc.returncode, "seconds": time.time() - t0,
            "kill_reason": kill_reason, "clips": found}


# ----------------------------------------------------------------------------
# entry points


def run(settings):
    """The supervised loop. Returns a process exit code."""
    global _STOP, _STOP_REQUESTS
    _STOP = False
    _STOP_REQUESTS = 0
    _normalize(settings)

    problems = settings.validate()
    if problems:
        print("[capture] this configuration cannot be used:")
        for p in problems:
            print("    - %s" % p)
        return 2
    if not os.path.isfile(_GENERATOR):
        print("[capture] generator not found: %s" % _GENERATOR)
        return 2

    os.makedirs(os.path.join(settings.output_dir, "clips"), exist_ok=True)

    try:
        plan = _variation_plan(settings)
    except Exception as exc:
        print("[capture] variation_configs() failed: %r" % (exc,))
        return 2
    per_scene = len(plan) or 1
    lifetime = _lifetime_clips(settings, per_scene)

    target = int(settings.target_clips or 0)
    print("[capture] output    %s" % settings.output_dir)
    print("[capture] video     %dx%d @ %d Hz, %.1f s clips (%.1f s lead discarded), "
          "chaos %.2f, graphics %s"
          % (settings.width, settings.height, settings.rate_hz,
             settings.clip_duration_s, settings.discard_lead_s,
             settings.chaos, settings.graphics))
    if plan:
        preset = _preset_name(settings)
        print("[capture] scenes    %d variation(s) per scene%s: %s"
              % (per_scene, " (preset %r)" % preset if preset else "",
                 ", ".join("%s (ego %s, actors %s)"
                           % (var.get("name"), _cell(var.get("ego_chaos")),
                              _cell(var.get("actor_chaos"))) for var, _ in plan)))
    print("[capture] target    %s, %s, %s per game lifetime"
          % ("%d kept clips" % target if target else "run until stopped",
             "%.1f h budget" % settings.max_hours if settings.max_hours > 0 else "no time limit",
             _fmt_chunk(lifetime, per_scene)))
    if lifetime != int(settings.clips_per_lifetime):
        print("[capture] lifetime  clips_per_lifetime %d -> %d: rounded up to whole "
              "scenes of %d, so a game death between chunks never leaves a scene "
              "half-captured" % (settings.clips_per_lifetime, lifetime, per_scene))
    print("[capture] game      %s:%d" % (settings.host, settings.port))

    manifest = Manifest(settings.output_dir)
    # Ids the log already carried BEFORE reconcile: anything reconcile has to
    # adopt is a clip a previous run captured but never finished with.
    known_before = set(manifest.known_clip_ids())
    recon = manifest.reconcile()
    print("[capture] reconcile %s" % _fmt_mapping(recon if isinstance(recon, dict) else {}))
    adopted = set(manifest.known_clip_ids()) - known_before

    kept = manifest.kept_count()
    if target:
        print("[capture] resuming  %d kept clip(s) already here, %d to go"
              % (kept, max(0, target - kept)))
        if kept >= target:
            print("[capture] target already met -- nothing to do")
            manifest.write_summary()
            _print_summary(manifest.summary(), settings.output_dir)
            return 0
    else:
        print("[capture] resuming  %d kept clip(s) already here" % kept)

    cfg_path, gen_cfg = _write_generator_config(settings)
    verdict = _check_generator_config(gen_cfg)
    print("[capture] generator %s -- %s" % (os.path.basename(cfg_path), verdict))
    if verdict.startswith("REJECTED"):
        print("[capture] refusing to launch: every chunk would fail identically")
        return 2

    game = Game(settings)
    game.apply_display_settings()

    finalizer = Finalizer(settings, manifest, workers=2)
    state = {"submitted": set(manifest.known_clip_ids()), "keep_ids": set(),
             "gen_encodes": bool(gen_cfg.get("video"))}
    if adopted:
        again = _resubmit_unfinished(settings, manifest, finalizer, state, adopted)
        if again:
            print("[capture] resuming  %d clip(s) from a previous run were never "
                  "finalized -- encoding/pruning them now" % len(again))

    prev_int = signal.signal(signal.SIGINT, _handle_stop)
    prev_term = signal.signal(signal.SIGTERM, _handle_stop)

    t_start = time.time()
    deadline = t_start + settings.max_hours * 3600.0 if settings.max_hours > 0 else None
    chunk = 0
    launch_fails = 0
    empty_chunks = 0
    force_next_launch = False
    code = 0

    try:
        while not _STOP:
            kept = _kept_estimate(manifest, state)
            if target and kept >= target:
                print("[capture] target reached: %d kept clip(s)" % kept)
                break
            if deadline and time.time() >= deadline:
                print("[capture] time budget spent (%s elapsed) at %d kept clip(s)"
                      % (_fmt_hms(time.time() - t_start), kept))
                break

            chunk += 1
            want = _chunk_size(settings, kept, target, per_scene)
            print("\n[capture] === chunk %d: %s, kept %s, elapsed %s ==="
                  % (chunk, _fmt_chunk(want, per_scene),
                     "%d/%d" % (kept, target) if target else str(kept),
                     _fmt_hms(time.time() - t_start)))

            if not game.ensure_running(deadline_s=_LAUNCH_DEADLINE_S,
                                       force=force_next_launch):
                launch_fails += 1
                print("[capture] the game would not come up (%d in a row)" % launch_fails)
                if launch_fails >= _MAX_LAUNCH_FAILS:
                    print("[capture] giving up: %d failed launches in a row is a broken "
                          "install or a locked Steam client, not a transient. Try "
                          "tools/gta_cleanup.sh and launching GTA V by hand."
                          % _MAX_LAUNCH_FAILS)
                    code = 1
                    break
                force_next_launch = True
                _sleep_interruptible(20.0)     # don't hammer the launcher
                continue
            launch_fails = 0
            force_next_launch = False

            seed = random.SystemRandom().randrange(1, 0x7FFFFFFF)
            res = _run_chunk(settings, cfg_path, want, seed, manifest, finalizer, state,
                             per_scene=per_scene)

            # ⚠ Only safe now the chunk has exited: is_producing() opens the control
            # socket, and ZMQ PAIR allows a single peer -- probing while a capture
            # client holds it fights with it.
            alive = game.is_alive()
            producing = alive and game.is_producing(timeout_s=20)
            if not alive:
                # Expected, roughly one chunk in three at scale. Not an error: the
                # next iteration relaunches and the run continues.
                state_note = "game DIED (expected occasionally; relaunching)"
            elif not producing:
                # ⚠ Alive is not producing. A hung game keeps its process up and its
                # socket bound while emitting no frames; reusing it cost an overnight
                # run seven whole chunks. Force the relaunch rather than trust it.
                state_note = "game alive but NOT producing -- forcing a relaunch"
                force_next_launch = True
            else:
                state_note = "game alive and producing"

            gained = len(res["clips"])
            gained_keep = sum(1 for _, k in res["clips"] if k)
            print("[capture] chunk %d: rc=%s in %s, +%d clip(s) (%d keep / %d reject), %s"
                  % (chunk, res["rc"], _fmt_hms(res["seconds"]), gained,
                     gained_keep, gained - gained_keep, state_note))
            if finalizer.pending():
                print("[capture]           %d clip(s) still finalizing in the background"
                      % finalizer.pending())

            if gained == 0:
                empty_chunks += 1
                force_next_launch = True
                if empty_chunks >= _MAX_EMPTY_CHUNKS:
                    print("[capture] giving up: %d chunks in a row produced no clip at "
                          "all. See %s for what the generator reported."
                          % (_MAX_EMPTY_CHUNKS,
                             os.path.join(settings.output_dir, "capture.log")))
                    code = 1
                    break
            else:
                empty_chunks = 0
    finally:
        # ⚠ The handlers stay installed through the drain. Restoring them here
        # would put the DEFAULT SIGINT back while the last clips are still
        # encoding, so an impatient second Ctrl-C would raise KeyboardInterrupt
        # out of drain() -- no summary, and clips left half-finalised. With ours
        # in place the extra Ctrl-C only sets a flag nothing is reading any more.
        # One last sweep first: a chunk cut short may have written its final clip
        # after the last in-chunk scan.
        _collect_new_clips(settings, manifest, finalizer, state, settle_s=0.0)
        pend = finalizer.pending()
        if pend:
            print("\n[capture] draining: %d clip(s) still encoding/pruning "
                  "(letting them finish; the files are not safe to leave half "
                  "written)..." % pend)
        try:
            finalizer.drain()
        finally:
            finalizer.close()
        try:
            manifest.write_summary()
        except Exception as exc:
            print("[capture] could not write summary.json (%r); manifest.jsonl is "
                  "intact and `--status` can rebuild it" % (exc,))
        # ★ The manifest IS the deliverable record, so check it against what was
        # actually captured rather than trusting it. Every clip counted here said
        # keep in its own meta.json; kept_count() also includes earlier runs, so
        # booking FEWER than that means finalisation is dropping good clips on the
        # floor -- and the loop's own target accounting reads the same number.
        observed = len(state["keep_ids"])
        booked = manifest.kept_count()
        if observed and booked < observed:
            print("[capture] ⚠ %d clip(s) captured this run say keep in their own "
                  "meta.json, but the manifest books only %d as kept. The record "
                  "disagrees with the clips -- see %s"
                  % (observed, booked, os.path.join(settings.output_dir, "capture.log")))
        print("[capture] ran %s, %d chunk(s)" % (_fmt_hms(time.time() - t_start), chunk))
        try:
            summary = manifest.summary()
            _print_summary(summary, settings.output_dir)
            _print_scene_report(summary, [var.get("name") for var, _ in plan])
        except Exception as exc:
            print("[capture] could not summarise the manifest (%r); the log itself "
                  "is at %s" % (exc, os.path.join(settings.output_dir, "manifest.jsonl")))
        print("[capture] the game is left running; tools/gta_cleanup.sh stops it.")
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)

    if _STOP and code == 0:
        return 130
    return code


def dry_run(settings):
    """Validate and show what a real run would do. Never touches the game.

    ⚠ Problems do NOT stop the report. A dry run is a diagnostic: bailing at the
    first bad field hides the resolved config, which is usually what the user
    opened it for -- and with the shipped example, whose paths say EDIT ME, it
    would print nothing else at all. The exit code still says no.
    """
    _normalize(settings)
    print("[capture] DRY RUN -- nothing is launched and nothing on disk is touched")
    print("[capture] output    %s" % settings.output_dir)
    print("[capture] video     %sx%s @ %s Hz, %s s clips (%s s lead discarded)"
          % (settings.width, settings.height, settings.rate_hz,
             settings.clip_duration_s, settings.discard_lead_s))
    print("[capture] chaos     %s    graphics %s    crf %s    keep_frames %s"
          % (settings.chaos, settings.graphics, settings.video_crf,
             settings.keep_frames))

    lib = _resolve_clip_library(settings)
    if settings.record_clip:
        print("[capture] clips     Rockstar Editor .clip per kept clip -> clip.clip "
              "(library %s)%s" % (lib or "NOT FOUND -- is the game installed and run once?",
                                  "" if lib else "  ⚠"))
    else:
        print("[capture] clips     Rockstar Editor recording off")
    print("[capture] mp4       %s" % (
        "encoded in the background (clip.mp4)" if settings.make_mp4 else
        "off -- render later from clip.clip with render_clip.py"
        + ("" if settings.record_clip or settings.keep_frames else "  ⚠ and no frames kept")))
    print("[capture] frames    %s" % (
        "captured (%s)" % ", ".join(n for n, on in (("mp4", settings.make_mp4),
                                                    ("keep_frames", settings.keep_frames)) if on)
        if (settings.make_mp4 or settings.keep_frames) else
        "NOT captured -- poses%s only; the game runs at real speed"
        % (" + .clip" if settings.record_clip else "")))

    # The plan is needed before the target line: it decides the lifetime size.
    # Its failure is reported as a problem rather than a traceback, like the rest.
    try:
        plan = _variation_plan(settings)
        plan_error = None
    except Exception as exc:
        plan, plan_error = [], "variation_configs() failed: %r" % (exc,)
    per_scene = len(plan) or 1
    try:
        lifetime = _lifetime_clips(settings, per_scene)
    except (TypeError, ValueError):
        lifetime = settings.clips_per_lifetime        # validate() reports it

    print("[capture] variations %s" % (
        "%d per scene: %s" % (per_scene, ", ".join(str(v.get("name")) for v, _ in plan))
        if plan else "off (one clip per location)"))
    print("[capture] target    %s, %s, %s per game lifetime"
          % ("%s kept clips" % settings.target_clips if settings.target_clips
             else "run until stopped",
             "%s h budget" % settings.max_hours if settings.max_hours
             else "no time limit",
             _fmt_chunk(lifetime, per_scene) if isinstance(lifetime, int)
             else "%s clip(s)" % (lifetime,)))
    if plan and lifetime != settings.clips_per_lifetime:
        print("[capture] lifetime  clips_per_lifetime %s -> %s: rounded up to whole "
              "scenes of %d, so a game death between chunks never leaves a scene "
              "half-captured" % (settings.clips_per_lifetime, lifetime, per_scene))
    print("[capture] game      %s:%s   %s" % (settings.host, settings.port,
                                              settings.gta_dir))

    problems = settings.validate()
    if plan_error:
        problems = problems + [plan_error]
    if problems:
        print("\n[capture] %d problem(s) -- a real run would refuse to start:"
              % len(problems))
        for p in problems:
            print("    - %s" % p)
    else:
        print("[capture] validate  OK")

    print("\n[capture] generator config (would be written to %s):"
          % _generator_config_path(settings))
    try:
        gen_cfg = settings.to_generator_config()
    except Exception as exc:
        print("    to_generator_config() failed: %r" % (exc,))
        gen_cfg = None
    if gen_cfg is not None:
        # The base config is printed in full; the plan is printed as a table below,
        # since N near-identical 100-line dicts are not something a person can diff.
        for line in json.dumps(gen_cfg, indent=2, sort_keys=True).splitlines():
            print("    " + line)
        if plan:
            gen_cfg[_PLAN_KEY] = [[var, gen] for var, gen in plan]
            print("    + %r: %d [variation, config] pair(s), see below" % (_PLAN_KEY, len(plan)))
        verdict = _check_generator_config(gen_cfg)
        print("[capture] %s" % verdict)
        if verdict.startswith("REJECTED"):
            # run() refuses to launch on this, so the dry run must not say ready.
            problems = problems + ["the generator would reject generator_config.json "
                                   "(see above); every chunk would fail identically"]
    if plan:
        _print_variation_plan(plan, settings)

    print("\n[capture] graphics values that would be written to settings.xml:")
    try:
        xml = settings.graphics_xml_values()
        for k in sorted(xml, key=lambda x: str(x)):
            print("    %-28s %s" % (k, xml[k]))
    except Exception as exc:
        print("    graphics_xml_values() failed: %r" % (exc,))

    if not os.path.isfile(_GENERATOR):
        print("\n[capture] generator MISSING: %s" % _GENERATOR)
        problems = problems + ["the longtail generator is not where it should be"]
    else:
        want = _chunk_size(settings, 0, int(settings.target_clips or 0), per_scene)
        budget = _chunk_budget_s(settings, want)
        cmd = _chunk_command(settings, _generator_config_path(settings), want, 123456)
        lock = _gpu_lock_script()
        print("\n[capture] first chunk: %s, %s budget, cwd %s"
              % (_fmt_chunk(want, per_scene), _fmt_hms(budget), _VPILOT))
        print("[capture] gpu lock: %s" % (lock if lock else "none (running unscheduled)"))
        print("    PYTHONPATH=%s" % _VPILOT)
        print("    " + " ".join(shlex.quote(a) for a in cmd))

    if os.path.isdir(settings.output_dir):
        m = Manifest(settings.output_dir)
        print("\n[capture] this output dir already holds %d manifest entr(ies), "
              "%d kept -- a real run would continue it, not restart it"
              % (len(m.load()), m.kept_count()))
    else:
        print("\n[capture] output dir does not exist yet; a real run would create it")

    if problems:
        print("[capture] VERDICT: cannot run -- fix the %d problem(s) above"
              % len(problems))
        return 2
    print("[capture] VERDICT: ready to run")
    return 0


def status(settings):
    """Print the manifest summary for the configured output dir."""
    _normalize(settings)
    if not os.path.isdir(settings.output_dir):
        print("[capture] no output dir yet: %s" % settings.output_dir)
        return 1
    manifest = Manifest(settings.output_dir)
    kept = manifest.kept_count()
    target = int(settings.target_clips or 0)
    print("[capture] output    %s" % settings.output_dir)
    if target:
        print("[capture] progress  %d/%d kept clip(s), %d to go"
              % (kept, target, max(0, target - kept)))
    else:
        print("[capture] progress  %d kept clip(s)" % kept)
    summary = manifest.summary()
    _print_summary(summary, settings.output_dir)

    # The configured plan supplies the row order and the "never captured" rows:
    # a variation with no clip yet is absent from the manifest altogether.
    try:
        plan = _variation_plan(settings)
    except Exception as exc:
        print("[capture] variation_configs() failed: %r" % (exc,))
        plan = []
    shown = _print_scene_report(summary, [var.get("name") for var, _ in plan])
    if plan and not shown:
        print("[capture] scenes    %d variation(s) per scene configured; no scene clip "
              "on record yet" % len(plan))
    return 0
