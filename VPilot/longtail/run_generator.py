#!/usr/bin/env python3
"""Long-tail clip generator. Runs until stopped (Ctrl-C) or max_clips reached.

    cd VPilot
    python -m longtail.run_generator --out F:/gtav_longtail

Resumes cleanly: the coverage ledger persists, so restarting continues covering
new ground instead of re-sampling the same places.

Counterfactual variations: when the JSON config carries a "variation_plan" (the
capture runner writes one from `variations` in capture.json), every proposed
location becomes one SCENE captured once per variation -- same place, weather,
clock, ego vehicle, ambient density, seeded population and staged incident;
different ego and actor behaviour. Clips are named "<scene_id>-<variation>",
each meta.json carries a "scene" block, and one line per scene goes to
<out_dir>/scenes.jsonl. A scene is never abandoned for budget reasons: --max-clips
is checked between scenes, so a run may overshoot it by up to N-1 clips.

⚠ A variation reproduces the setup, not the footage. GTA's ambient traffic and
its random ped/vehicle models are the game's own randomness; two variations are
never frame-identical replays.
"""

import argparse
import json
import os
import shutil
import random
import signal
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# VPilot/ for `deepgtav` and `utils`; VPilot/longtail/ for sibling modules.
for _p in (os.path.dirname(_HERE), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from deepgtav.client import Client
from deepgtav.messages import (Dataset, Scenario as GtaScenario, SetCapturePause,
                               SetGameplayCamView, Start, Stop, StartRecording)

# ⚠ NOT `from longtail import writer` above the sys.path repair: that only
# worked when the caller had already put VPilot on PYTHONPATH, and died with
# ModuleNotFoundError from a bare `python3 longtail/run_generator.py`.
import writer as writer_mod
import drivingstyles as ds
import outcomes
from config import CaptureConfig
from coverage import CoverageSampler
from episode import EpisodeRunner

_STOP = False

#: Consecutive run_clip() exceptions before the run gives up. A failed clip used
#: to increment neither counter, so `max_clips` was unreachable and a persistent
#: fault span forever; three in a row is structural, not a one-off.
_MAX_CONSECUTIVE_FAILURES = 3


def _handle_sigint(signum, frame):
    global _STOP
    print("\n[longtail] stop requested; finishing current clip...")
    _STOP = True


# ---------------------------------------------------------------------------
# variation plan
# ---------------------------------------------------------------------------

def _variation_plan(cfg):
    """[(Variation, CaptureConfig), ...] from cfg.variation_plan; [] when off.

    Each pair in the JSON is [Variation, generator_config_dict]. The dict is a
    complete generator config as the runner merged it for that variation, but it
    was written BEFORE this process applied --out / --seed / --max-clips, so the
    variation's config is rebuilt on top of `cfg` (clone) and the run-level
    fields are then pinned back to the run's. An unknown key in a variation dict
    raises here, at startup, rather than on the first clip.
    """
    raw = getattr(cfg, "variation_plan", None) or []
    plan = []
    names = set()
    for i, item in enumerate(raw):
        try:
            var, gen = item
        except (TypeError, ValueError):
            raise ValueError("variation_plan[%d] is not a [Variation, config] pair: %r"
                             % (i, item))
        var = dict(var)
        name = str(var.get("name") or "")
        if not name:
            raise ValueError("variation_plan[%d] has no name" % i)
        if name in names:
            raise ValueError("variation_plan has two variations named %r" % name)
        names.add(name)
        vcfg = cfg.clone(**dict(gen))
        vcfg.out_dir = cfg.out_dir
        vcfg.seed = cfg.seed
        vcfg.max_clips = cfg.max_clips
        # ⚠ A variation's config must not itself carry a plan: clone() copied the
        # parent's list, and anything reading variation_plan off a per-variation
        # config would then believe it planned N variations of its own.
        vcfg.variation_plan = []
        plan.append(({"name": name,
                      "ego_chaos": var.get("ego_chaos"),
                      "actor_chaos": var.get("actor_chaos")}, vcfg))
    return plan


# ---------------------------------------------------------------------------
# per-clip bookkeeping, shared by both paths
# ---------------------------------------------------------------------------

def _finish_clip(cfg, meta, region, bias, index_path, scene_id=None, variation=None):
    """Everything that happens to a clip after run_clip() returns: outcome model,
    video encode + prune, index line. Identical for a single clip and for one
    variation of a scene; the scene fields are null on the single-clip path."""
    label = meta["outcome"]["label"]
    # Only successful clips inform the outcome model -- a clip dropped for
    # pop-in tells us nothing about what the scenario yields.
    if meta["keep"]:
        bias.record(meta["scenario"]["name"], label)
        if getattr(cfg, "video", False):
            # ⚠ meta has clip_id, not dir -- and an encode problem must never
            # take down a capture run that already has good frames on disk.
            clip_dir = os.path.join(cfg.out_dir, "clips", meta["clip_id"])
            try:
                vid = writer_mod.encode_video(clip_dir, cfg, cfg.image_format)
                if vid:
                    meta["video"] = vid
                    # ⚠ meta.json was already written by writer.finalize(),
                    # before the encode ran. Setting meta["video"] here only
                    # touched the in-memory dict, so the video block -- true
                    # vs approximate timing, the real sampling range, size --
                    # was computed and thrown away on every clip.
                    with open(os.path.join(clip_dir, "meta.json"), "w") as mf:
                        json.dump(meta, mf, indent=2)
                    if getattr(cfg, "video_only", False):
                        shutil.rmtree(os.path.join(clip_dir, "frames"),
                                      ignore_errors=True)
            except Exception as exc:
                print("[longtail] video encode failed (frames kept): %r" % (exc,))
        bias.save()
    elif getattr(cfg, "video_only", False):
        # ⚠ video_only only ever pruned KEPT clips, so every rejected clip
        # left 251 MB of frames on disk forever -- at a 25% reject rate
        # that is as much storage as the kept dataset itself. A rejected
        # clip's frames are by definition not wanted; meta.json and
        # poses.jsonl stay, so the rejection is still auditable.
        shutil.rmtree(os.path.join(cfg.out_dir, "clips", meta["clip_id"],
                                   "frames"), ignore_errors=True)

    with open(index_path, "a") as f:
        f.write(json.dumps({
            "clip_id": meta["clip_id"], "keep": meta["keep"],
            "scenario": meta["scenario"]["name"], "region": region,
            "outcome": label, "frames": meta["frames"],
            "warmup_s": meta["timing"]["warmup_s"],
            "reasons": meta["reject_reasons"],
            "scene_id": scene_id, "variation": variation,
        }) + "\n")


def _record_coverage(sampler, meta, x, y, region):
    snapped = (meta.get("placement") or {}).get("snapped_xy")
    if snapped:
        sampler.record(snapped[0], snapped[1], region)
    else:
        sampler.record(x, y, region)
    sampler.save()


def _verdict_str(meta):
    return "KEEP" if meta["keep"] else "DROP:" + ",".join(meta["reject_reasons"])


# ---------------------------------------------------------------------------
# the two capture loops
# ---------------------------------------------------------------------------

def _run_single_clips(runner, cfg, sampler, bias, index_path):
    """One proposed location -> one clip. The original loop, unchanged in effect."""
    kept = rejected = failed = consec_failed = 0
    t_start = time.time()

    # One-deep lookahead so the NEXT clip's location can be pre-streamed during
    # the current one (PrepareLocation). This is what lets warmup be ~1 s.
    pending = sampler.propose()

    while not _STOP:
        if cfg.max_clips and (kept + rejected + failed) >= cfg.max_clips:
            break
        x, y, region = pending
        nxt = sampler.propose()
        try:
            meta = runner.run_clip(x, y, region, cfg.out_dir,
                                   next_xy=(nxt[0], nxt[1]) if cfg.pipeline_next_location else None)
        except Exception as exc:                      # keep the run alive
            failed += 1
            consec_failed += 1
            print("[longtail] clip failed (%d consecutive): %r" % (consec_failed, exc))
            pending = nxt
            if consec_failed >= _MAX_CONSECUTIVE_FAILURES:
                print("[longtail] aborting: %d consecutive failures" % _MAX_CONSECUTIVE_FAILURES)
                break
            continue
        consec_failed = 0
        pending = nxt

        _record_coverage(sampler, meta, x, y, region)
        _finish_clip(cfg, meta, region, bias, index_path)

        if meta["keep"]:
            kept += 1
        else:
            rejected += 1
        rate = (kept + rejected) / max(1e-6, (time.time() - t_start) / 3600.0)
        print("[longtail] %-22s %-14s %-16s warm=%4.1fs frames=%-4d %s  (kept %d / rej %d, %.0f clips/h)"
              % (meta["scenario"]["name"], meta["outcome"]["label"], region,
                 meta["timing"]["warmup_s"], meta["frames"], _verdict_str(meta),
                 kept, rejected, rate))
    return {"kept": kept, "rejected": rejected, "failed": failed}


def _run_scenes(runner, cfg, plan, sampler, bias, index_path, scenes_path, rng,
                max_scenes=0):
    """One proposed location -> one SCENE -> one clip per variation.

    The scene is built once (env sampled, scenario chosen, id and seed minted)
    and enters the coverage ledger once. Every variation then relocates to the
    same (x, y) with the same scene, differing only in the config it is given.
    """
    kept = rejected = failed = consec_failed = 0
    scenes = complete_scenes = 0
    n = len(plan)
    t_start = time.time()
    pending = sampler.propose()

    while not _STOP:
        # ★ Budget is checked at scene boundaries only. A scene stopped two
        # variations in is worth less than the clips it cost, so the scene in
        # progress always finishes; the overshoot is bounded by N-1 clips.
        if cfg.max_clips and (kept + rejected + failed) >= cfg.max_clips:
            break
        if max_scenes and scenes >= max_scenes:
            break
        x, y, region = pending
        nxt = sampler.propose()
        scene = runner.build_scene(x, y, region, rng, n_variations=n)
        sid = scene["scene_id"]
        print("[longtail] scene %s  %-14s %-24s (%.0f, %.0f)  x%d"
              % (sid, region, scene["scenario"], x, y, n))

        results = []
        recorded = False
        structural_failure = False
        for i, (var, vcfg) in enumerate(plan):
            if _STOP:
                break                      # Ctrl-C: the scene is written incomplete
            name = var["name"]
            clip_id = "%s-%s" % (sid, name)
            # PrepareLocation is for the NEXT place. Inside a scene the next place
            # is this one, already streamed, so only the last variation pre-streams.
            last = (i == n - 1)
            next_xy = (nxt[0], nxt[1]) if (last and cfg.pipeline_next_location) else None
            try:
                meta = runner.run_clip(x, y, region, cfg.out_dir, next_xy=next_xy,
                                       scene=scene, variation=var, gen_cfg=vcfg,
                                       variation_index=i)
            except Exception as exc:                  # keep the run alive
                failed += 1
                consec_failed += 1
                print("[longtail]   %-16s FAILED (%d consecutive): %r"
                      % (name, consec_failed, exc))
                results.append({"name": name, "clip_id": clip_id, "kept": False,
                                "label": None, "error": repr(exc)})
                if consec_failed >= _MAX_CONSECUTIVE_FAILURES:
                    structural_failure = True
                    break
                continue
            consec_failed = 0

            # The ledger records the scene ONCE, from the first variation that
            # produced a clip -- the plugin road-snaps the same request to the
            # same node, so every variation's snapped point is the same point.
            if not recorded:
                _record_coverage(sampler, meta, x, y, region)
                recorded = True
            _finish_clip(cfg, meta, region, bias, index_path, scene_id=sid, variation=name)

            if meta["keep"]:
                kept += 1
            else:
                rejected += 1
            results.append({"name": name, "clip_id": meta["clip_id"],
                            "kept": bool(meta["keep"]),
                            "label": meta["outcome"]["label"]})
            print("[longtail]   %-16s %-14s warm=%4.1fs frames=%-4d %s"
                  % (name, meta["outcome"]["label"], meta["timing"]["warmup_s"],
                     meta["frames"], _verdict_str(meta)))

        # complete: every variation reached a verdict (kept OR rejected). That is
        # "the run is done with this scene", not "the whole grid is on disk" --
        # the per-variation `kept` flags say which clips survived. A scene cut
        # short by a game death, a failure run or Ctrl-C is written anyway, so
        # its clips can still be accounted for; complete=false is the record.
        complete = (len(results) == n and all("error" not in r for r in results))
        scenes += 1
        if complete:
            complete_scenes += 1
        n_kept = sum(1 for r in results if r["kept"])
        with open(scenes_path, "a") as f:
            f.write(json.dumps({
                "scene_id": sid, "region": region, "scenario": scene["scenario"],
                "x": scene["x"], "y": scene["y"], "scene_seed": scene["scene_seed"],
                # ⚠ "variations" is the PLAN (every name that should run), not the
                # results. manifest._scene_progress reads this key as the plan and
                # checks that every listed name has a clip; writing only the ones
                # that ran made an interrupted scene read as complete -- a scene
                # stopped at 2 of 4 reported scenes_incomplete=0. Results go under
                # their own key.
                "n_variations": n, "variations": [v["name"] for v, _ in plan],
                "results": results, "complete": complete,
            }) + "\n")

        # ★ The line the feature exists for: one scene, every variation's
        # outcome side by side.
        cells = []
        for r in results:
            if "error" in r:
                cells.append("%s=FAILED" % r["name"])
            elif r["kept"]:
                cells.append("%s=%s" % (r["name"], r["label"]))
            else:
                cells.append("%s=%s(DROP)" % (r["name"], r["label"]))
        for var, _vcfg in plan[len(results):]:
            cells.append("%s=-" % var["name"])
        rate = (kept + rejected) / max(1e-6, (time.time() - t_start) / 3600.0)
        print("[longtail] scene %s  %-14s %-24s %s  [%d/%d kept%s, %.0f clips/h]"
              % (sid, region, scene["scenario"], "  ".join(cells), n_kept, n,
                 "" if complete else ", INCOMPLETE", rate))

        pending = nxt
        if structural_failure:
            print("[longtail] aborting: %d consecutive failures" % _MAX_CONSECUTIVE_FAILURES)
            break
    return {"kept": kept, "rejected": rejected, "failed": failed,
            "scenes": scenes, "complete_scenes": complete_scenes}


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--out", default=None, help="output dir (overrides config)")
    ap.add_argument("--config", default=None, help="JSON config override")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--max-clips", type=int, default=None,
                    help="stop after this many clips (kept + rejected + failed); "
                         "with variations, checked between scenes, so the run "
                         "finishes the scene it is in")
    ap.add_argument("--max-scenes", type=int, default=0,
                    help="with variations: stop after this many scenes (0 = no limit)")
    ap.add_argument("--d-min", type=float, default=140.0,
                    help="minimum separation (m) between clip start points")
    args = ap.parse_args()

    cfg = CaptureConfig.load(args.config)
    if args.out:
        cfg.out_dir = args.out
    if args.seed is not None:
        cfg.seed = args.seed
    if args.max_clips is not None:
        cfg.max_clips = args.max_clips

    # ⚠ seed=0 means "pick one", NOT "use zero" -- random.Random(0) is a perfectly
    # deterministic sequence. A pinned seed made every batch replay the same
    # locations, vehicles and scenarios in the same order, which defeats the whole
    # point of maximising scene variation. Derive from the clock and RECORD it, so
    # a run stays reproducible after the fact.
    if not cfg.seed:
        cfg.seed = int(time.time()) & 0x7FFFFFFF
        print("[longtail] seed not pinned; using %d" % cfg.seed)

    # After every run-level override has landed on cfg, so each variation's
    # config is re-based on the run as it will actually execute.
    plan = _variation_plan(cfg)
    if plan:
        print("[longtail] variations: %d per scene -- %s"
              % (len(plan), ", ".join("%s(ego %.2f, actors %.2f)"
                                      % (v["name"], v["ego_chaos"] or 0.0,
                                         v["actor_chaos"] or 0.0) for v, _c in plan)))

    os.makedirs(cfg.out_dir, exist_ok=True)
    cfg.save(os.path.join(cfg.out_dir, "capture_config.json"))

    rng = random.Random(cfg.seed)
    # [longtail] Optional per-run region weighting. coverage.REGIONS carries the
    # default (hard urban), but a validation run or a targeted top-up needs to aim
    # at a specific part of the map without editing coverage.py.
    _regions = None
    if getattr(cfg, "region_weights", None):
        from coverage import REGIONS as _R
        _regions = {k: (v[0], v[1], v[2], v[3], float(cfg.region_weights.get(k, v[4])))
                    for k, v in _R.items()
                    if float(cfg.region_weights.get(k, v[4])) > 0.0}
        print("[longtail] region weights overridden: %s"
              % {k: v[4] for k, v in _regions.items()})
    sampler = CoverageSampler(os.path.join(cfg.out_dir, "coverage_ledger.json"),
                              d_min=args.d_min, seed=cfg.seed, regions=_regions)
    bias = outcomes.OutcomeBiased(os.path.join(cfg.out_dir, "outcome_ledger.json"),
                                  enabled=cfg.outcome_bias,
                                  temperature=cfg.outcome_bias_temperature,
                                  min_history=cfg.outcome_bias_min_history)
    index_path = os.path.join(cfg.out_dir, "clips_index.jsonl")
    scenes_path = os.path.join(cfg.out_dir, "scenes.jsonl")

    signal.signal(signal.SIGINT, _handle_sigint)

    print("[longtail] connecting to DeepGTAV at %s:%d" % (args.host, args.port))
    # ⚠ Without a timeout a dead/crashed game leaves recv() blocking forever: the
    # run looks alive, holds the GPU lock, and produces nothing.
    client = Client(ip=args.host, port=args.port, recv_timeout_ms=120000)

    # [rockstar] ⚠ Before the first capture cycle, not after: the replay recorder
    # latches on the FIRST SET_GAME_PAUSED it sees in a process. With .clip
    # recording on, the plugin captures under SET_TIME_SCALE(0) alone.
    if getattr(cfg, "record_clip", False):
        client.sendMessage(SetCapturePause(enabled=False))
        if str(getattr(cfg, "clip_camera", "first_person")).lower() == "first_person":
            # The replay records the GAMEPLAY camera, not our script cam: make
            # that the driver's view so the .clip plays back the way it was seen.
            client.sendMessage(SetGameplayCamView(mode=4))
            print("[longtail] rockstar editor recording: gameplay camera set to first person")
        print("[longtail] rockstar editor recording ON: capture pause disabled, "
              "library %s" % (getattr(cfg, "clip_library_dir", "") or "(auto)"))

    # One Start; every clip afterwards is a Config, which rebuilds the scenario
    # in place (Scenario::config also calls buildScenario).
    client.sendMessage(Start(
        scenario=GtaScenario(location=[0.0, 0.0], time=[12, 0], weather="CLEAR",
                             vehicle=cfg.ego_vehicles[0],
                             drivingMode=[ds.NORMAL, cfg.ego_cruise_speed]),
        dataset=Dataset(rate=cfg.rate_hz, speed=True, location=True, time=True,
                        frame=[cfg.width, cfg.height],
                        screenResolution=[cfg.screen_width, cfg.screen_height],
                        captureFrames=bool(getattr(cfg, "capture_frames", True)))))
    if not getattr(cfg, "capture_frames", True):
        print("[longtail] frames OFF: poses%s only, no backbuffer capture, game time at wall time"
              % (" + .clip" if getattr(cfg, "record_clip", False) else ""))

    # ⚠ recording_active defaults to FALSE in DataExport.h. Without StartRecording
    # the plugin never calls capture(), and every frame the client receives is the
    # freshly-malloc'd zero buffer -- i.e. a full run of pure black frames with
    # valid poses, valid metadata and quality gates all reporting success.
    # (messages.py labels this "TODO not yet implemented"; Server.cpp:114 does
    # implement it. The comment is stale.)
    client.sendMessage(StartRecording())
    print("[longtail] recording enabled")

    runner = EpisodeRunner(client, cfg, rng, bias=bias)
    totals = {"kept": 0, "rejected": 0, "failed": 0}

    try:
        if plan:
            totals = _run_scenes(runner, cfg, plan, sampler, bias, index_path,
                                 scenes_path, rng, max_scenes=args.max_scenes)
        else:
            totals = _run_single_clips(runner, cfg, sampler, bias, index_path)
    finally:
        try:
            client.sendMessage(Stop())
            client.close()
        except Exception:
            pass
        sampler.save()
        bias.save()
        rep = bias.report()
        print("\n[longtail] done. kept=%d rejected=%d" % (totals["kept"], totals["rejected"]))
        if "scenes" in totals:
            print("[longtail] scenes: %d, complete %d  (see %s)"
                  % (totals["scenes"], totals["complete_scenes"], scenes_path))
        print("[longtail] coverage: %s" % json.dumps(sampler.stats()["region_counts"]))
        print("[longtail] outcomes: %s" % json.dumps(rep["outcome_totals"]))
        print("[longtail] outcome entropy: %.3f / %.3f bits"
              % (rep["entropy_bits"], rep["max_bits"]))
        for name, y in sorted(rep["per_scenario"].items(), key=lambda kv: -kv[1]["event_rate"]):
            print("            %-24s clips=%-4d event_rate=%.2f  top=%s"
                  % (name, y["clips"], y["event_rate"], y["top"]))


if __name__ == "__main__":
    main()
