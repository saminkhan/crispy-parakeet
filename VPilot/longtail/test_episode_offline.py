"""Offline dry-run of the episode state machine against a simulated plugin.

Proves the clip loop, warmup gating, trigger scheduling, monotonic-time filtering
and quality gates work without GTA V -- and that counterfactual variations of one
scene land at the same place, stage the same incident and differ only in
behaviour. It does NOT prove any native call is correct -- only that the harness
around them behaves.

    python longtail/test_episode_offline.py     (from VPilot/)
"""
import os
import random
import sys
import tempfile

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_HERE), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import posemath


class FakePlugin:
    """Simulates the DeepGTAV message stream: a car driving with a nose-mounted cam."""

    def __init__(self, rate_hz=20, jitter=0.35, reorder_every=0, warm_frames=40,
                 ready_after=3, crash_at=None, fade_at=None, eject_at=None):
        self.dt = 1.0 / rate_hz
        self.jitter = jitter
        self.reorder_every = reorder_every
        self.t_ms = 1000
        self.n = 0
        self.warm_frames = warm_frames
        self.ready_after = ready_after
        self.crash_at = crash_at
        self.fade_at = fade_at
        self.eject_at = eject_at
        self.pos = np.array([1200.0, -800.0, 30.0])
        self.heading = 42.0
        self.sent = []
        # The objects themselves, alongside the type names in `sent`: the
        # variation test has to compare Config locations and SetActorBehaviour
        # payloads across clips, not just count that they were sent.
        self.messages = []
        self.teleports = 0
        self.gen = 0
        self.rng = random.Random(11)

    def sendMessage(self, m):
        self.sent.append(type(m).__name__)
        self.messages.append(m)
        # ★ Simulate the RELOCATE. episode._warmup() will not release a clip until
        # it has seen the ego actually arrive at the requested location -- detected
        # as a position discontinuity, because the plugin road-snaps the request and
        # can land hundreds of metres away. A fake that never teleports leaves
        # `arrived` false forever, so every clip burned the full arrive_timeout_s
        # and the test proved nothing while appearing to hang.
        if type(m).__name__ == "Config":
            sc = getattr(m, "scenario", None)
            loc = getattr(sc, "location", None) if sc is not None else None
            if loc:
                # Land ~40 m off the requested point, as a road-snap would.
                self.pos = np.array([float(loc[0]) + 40.0, float(loc[1]) - 25.0, 30.0])
                self.teleports += 1
                self.gen += 1
        return True

    def recvMessage(self):
        self.n += 1
        speed = 0.0 if self.n < self.warm_frames else 18.0
        crashed = self.crash_at is not None and self.n >= self.crash_at
        if crashed:
            speed = max(0.0, 18.0 - (self.n - self.crash_at) * 4.0)
        self.heading += self.rng.gauss(0, 1.2)
        theta = [self.rng.gauss(0, 0.8), self.rng.gauss(0, 0.3), self.heading]
        _r, _u, fwd = posemath.camera_basis(theta)
        self.pos = self.pos + fwd * speed * self.dt
        self.t_ms += int(self.dt * 1000 * (1.0 + self.rng.uniform(-self.jitter, self.jitter)))
        t = self.t_ms
        if self.reorder_every and self.n % self.reorder_every == 0:
            t = self.t_ms - 500          # a straggler from the past
        return {"CameraPosition": self.pos.tolist(), "CameraAngle": theta,
                "CameraFOV": 50.0, "CameraAspectRatio": 1920 / 1080,
                "CameraNearClip": 0.15, "CameraFarClip": 1500.0,
                "GameTime": t, "speed": speed, "frame": None,
                # --- longtail ego state ---
                "EgoSpeed": speed,
                "EgoCollided": crashed,
                "EgoOnFire": False,
                "ScenarioGen": self.gen,
                "EgoAtLight": False,
                "EgoHealth": 1000,
                "EgoEngineHealth": 1000.0,
                "EgoBodyHealth": 600.0 if crashed else 1000.0,
                "EgoTankHealth": 1000.0,
                "EgoRoll": 0.0, "EgoPitch": 0.0, "EgoOnRoof": False,
                "EgoPedHealth": 200,
                "EgoPedInVehicle": not (self.eject_at is not None and self.n >= self.eject_at),
                "ScreenFaded": bool(self.fade_at is not None and self.n >= self.fade_at),
                "WorldReady": self.n >= self.ready_after,
                "SceneStreamed": self.n >= self.ready_after,
                "RoadNodeValid": True, "RoadNodeDensity": 7, "RoadNodeFlags": 0}

    def close(self):
        pass


def run(reorder_every=0, clips=3, **plugin_kw):
    import config as cfgmod
    import coverage
    import episode
    import outcomes as outmod
    import writer as writermod

    # discard_lead_s=0 here on purpose: these cases exercise outcome labelling and
    # the guards, and a lead would push the synthetic collision outside the written
    # clip (where it correctly becomes no_event). The lead itself is covered by its
    # own assertion below.
    cfg = cfgmod.CaptureConfig(width=64, height=36, clip_seconds_min=4.0,
                               discard_lead_s=0.0,
                               clip_seconds_max=6.0, warmup_seconds_min=0.0,
                               warmup_seconds_max=1.0, warmup_min_speed=0.0,
                               out_dir=tempfile.mkdtemp(), image_format="png")

    # No cv2 here: bypass encoding, keep every other code path intact.
    class NoIOWriter(writermod.ClipWriter):
        def add_frame(self, image_bgr, pose, extra=None):
            import json
            row = {"i": self.n, "game_time_ms": pose["game_time_ms"],
                   "position": pose["position"], "theta_deg": pose["theta_deg"],
                   "fov_deg": pose["fov_deg"]}
            if extra:
                row.update(extra)
            self._pose_fh.write(json.dumps(row) + "\n")
            self._update_quality(image_bgr, pose, self.n)
            if self.first_pose is None:
                self.first_pose = pose
            self.last_pose = pose
            self.n += 1
            return self.n - 1

    episode.ClipWriter = NoIOWriter

    plugin = FakePlugin(reorder_every=reorder_every, warm_frames=5, **plugin_kw)
    rng = random.Random(5)
    sampler = coverage.CoverageSampler(os.path.join(cfg.out_dir, "led.json"), seed=5)
    bias = outmod.OutcomeBiased(os.path.join(cfg.out_dir, "out.json"))
    runner = episode.EpisodeRunner(plugin, cfg, rng, bias=bias)

    results = []
    pending = sampler.propose()
    for _ in range(clips):
        x, y, region = pending
        nxt = sampler.propose()
        meta = runner.run_clip(x, y, region, cfg.out_dir, next_xy=(nxt[0], nxt[1]))
        pending = nxt
        sampler.record(x, y, region)
        if meta["keep"]:
            bias.record(meta["scenario"]["name"], meta["outcome"]["label"])
        results.append(meta)
    return cfg, results, plugin, bias


def outcomes_labels():
    import outcomes as o
    return o.LABELS


if __name__ == "__main__":
    import json

    cfg, results, plugin, bias = run()
    print("=== normal stream ===")
    for m in results:
        s = m["scenario"]
        print("  %-24s %-14s frames=%-4d warm=%.2fs trigger@%.2fs (frame %s) keep=%s"
              % (s["name"], m["outcome"]["label"], m["frames"], m["timing"]["warmup_s"],
                 s["trigger_at_s"], s["trigger_frame"], m["keep"]))
        assert m["frames"] > 10, "too few frames"
        assert s["trigger_frame"] is not None, "trigger never fired"
        assert 0 < s["trigger_frame"] < m["frames"], "trigger outside the clip"
        assert m["intrinsics"]["fx"] > 0
        assert m["timing"]["world_ready"], "world_ready gate did not resolve"
        assert m["outcome"]["label"] in outcomes_labels(), "bad outcome label"

    d = os.path.join(cfg.out_dir, "clips", results[0]["clip_id"])
    ts = [json.loads(l)["game_time_ms"] for l in open(os.path.join(d, "poses.jsonl"))]
    assert all(b > a for a, b in zip(ts, ts[1:])), "game_time not monotonic"
    print("  monotonic game_time: OK (%d frames)" % len(ts))

    assert "PrepareLocation" in plugin.sent, "next location was never pre-streamed"
    assert "SetSceneDensity" in plugin.sent, "density never applied"
    assert "SetSurvivalMode" in plugin.sent, "capture guards never applied"
    print("  pipelined PrepareLocation / density / guards: OK")

    print("\n=== reordered messages every 7th ===")
    _c, r2, _p, _b = run(reorder_every=7, clips=2)
    for m in r2:
        ooo = m["timing"]["out_of_order_messages"]
        print("  %-24s frames=%-4d out_of_order_dropped=%d" % (m["scenario"]["name"], m["frames"], ooo))
        assert ooo > 0, "reordering was not detected"

    print("\n=== collision detected and labelled ===")
    _c, r3, _p, _b = run(clips=1, crash_at=25)
    m = r3[0]
    print("  label=%s  decel=%.1f m/s2  body_drop=%.0f"
          % (m["outcome"]["label"], m["outcome"]["measurements"]["peak_decel_mps2"],
             m["outcome"]["measurements"]["body_health_drop"]))
    assert m["outcome"]["label"] in ("major_collision", "minor_contact"), m["outcome"]

    print("\n=== game-over guard failures must ABORT, not be written ===")
    _c, r4, _p, _b = run(clips=1, fade_at=30)
    assert not r4[0]["keep"], "faded clip was kept"
    assert "game-over" in " ".join(r4[0]["reject_reasons"]), r4[0]["reject_reasons"]
    print("  screen fade  -> DROP: %s" % r4[0]["reject_reasons"])

    _c, r5, _p, _b = run(clips=1, eject_at=30)
    assert not r5[0]["keep"], "ejected-ped clip was kept"
    assert "POV integrity" in " ".join(r5[0]["reject_reasons"]), r5[0]["reject_reasons"]
    print("  ped ejected  -> DROP: %s" % r5[0]["reject_reasons"])

    print("\nAll offline episode tests passed.")



# --- discard_lead_s: frames before the lead must never be written -------------
def test_discard_lead():
    """The lead is captured (so the world settles and the ego gets up to speed)
    but never written. The delivered clip must start at t=0 and last `duration`."""
    import json as _json, shutil as _shutil
    import config as cfgmod, coverage, episode, writer as writermod

    out = tempfile.mkdtemp(prefix="leadtest_")
    try:
        cfg = cfgmod.CaptureConfig(width=64, height=36,
                                   clip_seconds_min=6.0, clip_seconds_max=6.0,
                                   discard_lead_s=3.0,
                                   warmup_seconds_min=0.0, warmup_seconds_max=1.0,
                                   warmup_min_speed=0.0, out_dir=out,
                                   image_format="png")

        class NoIOWriter(writermod.ClipWriter):
            def add_frame(self, image_bgr, pose, extra=None):
                import json
                row = {"i": self.n, "game_time_ms": pose["game_time_ms"],
                       "position": pose["position"], "theta_deg": pose["theta_deg"],
                       "fov_deg": pose["fov_deg"]}
                if extra:
                    row.update(extra)
                self._pose_fh.write(json.dumps(row) + "\n")
                self._update_quality(image_bgr, pose, self.n)
                if self.first_pose is None:
                    self.first_pose = pose
                self.last_pose = pose
                self.n += 1
                return self.n - 1
        episode.ClipWriter = NoIOWriter

        plugin = FakePlugin(warm_frames=5)
        runner = episode.EpisodeRunner(plugin, cfg, random.Random(3))
        sampler = coverage.CoverageSampler(os.path.join(out, "led.json"), seed=3)
        x, y, region = sampler.propose()
        meta = runner.run_clip(x, y, region, out)
        d = os.path.join(out, "clips", meta["clip_id"])
        rows = [_json.loads(l) for l in open(os.path.join(d, "poses.jsonl"))]
        assert rows, "no frames written"
        ts = [r["t"] for r in rows]
        span = (rows[-1]["game_time_ms"] - rows[0]["game_time_ms"]) / 1000.0
        assert min(ts) >= -1e-6, "clip must start at t=0, got %.3f" % min(ts)
        assert span <= 6.0 + 1.0, "written span %.2fs exceeds duration 6.0s" % span
        assert span >= 6.0 - 2.0, "written span %.2fs far short of duration" % span
        print("  lead discarded: written t %.2f..%.2f, span %.2fs (3.0s lead dropped)"
              % (min(ts), max(ts), span))
    finally:
        _shutil.rmtree(out, ignore_errors=True)


print("discard-lead test passed.")


# --- counterfactual variations: N clips of ONE scene ---------------------------
def test_variations_share_scene():
    """Two variations of one scene must land at the same place, stage the same
    incident under the same conditions, and differ only in behaviour.

    Written against the scene/variation contract:
      run_clip(x, y, region, out_dir, scene=<Scene dict>, variation=<Variation dict>)
      clip_id "<scene_id>-<name>"; meta.json gains a "scene" block; the
      environment block records the ego_chaos / actor_chaos used.

    The two behaviour configs are built by hand from CaptureConfig -- the same
    EGO/ACTOR knobs CaptureSettings.variation_configs() would move for ego_chaos /
    actor_chaos 0.0 and 1.0 -- so this file stays free of capture/ imports like the
    rest of it. Every SCENE knob is left identical between the two, as it must be.
    """
    import json as _json, shutil as _shutil
    import config as cfgmod, episode, writer as writermod

    out = tempfile.mkdtemp(prefix="scenetest_")
    try:
        base = cfgmod.CaptureConfig(width=64, height=36,
                                    clip_seconds_min=4.0, clip_seconds_max=6.0,
                                    discard_lead_s=0.0,
                                    warmup_seconds_min=0.0, warmup_seconds_max=1.0,
                                    warmup_min_speed=0.0, out_dir=out,
                                    image_format="png")
        # actor_chaos 0.0 / ego_chaos 0.0: NORMAL traffic that stops and steers
        # around, no crossings, no forced fires, a careful ego.
        calm = base.clone(
            npc_driving_style=786603,           # drivingstyles.NORMAL
            npc_aggressiveness=0.0, npc_ability=1.0, npc_cruise_speed=20.0,
            npc_steers_around=True, traffic_aggression=False,
            ped_interaction=False, ped_cross_chance=0.0, ignite_wrecks=False,
            ttc_min=3.0, ttc_max=6.0,
            ego_aggressiveness_min=0.0, ego_aggressiveness_max=0.1,
            ego_ability_min=0.9, ego_ability_max=1.0,
            driving_style_bit_p=0.95, driving_style_license_p=0.0)
        # actor_chaos 1.0 / ego_chaos 1.0: CaptureConfig's own defaults, which ARE
        # the tuned max-chaos values, spelled out so the diff is visible here.
        chaos = base.clone(
            npc_driving_style=512 | 262144,     # ALLOW_WRONG_WAY | TAKE_SHORTEST_PATH
            npc_aggressiveness=1.0, npc_ability=0.0, npc_cruise_speed=40.0,
            npc_steers_around=False, traffic_aggression=True,
            ped_interaction=True, ped_cross_chance=0.35, ignite_wrecks=True,
            ttc_min=0.3, ttc_max=0.9,
            ego_aggressiveness_min=0.95, ego_aggressiveness_max=1.0,
            ego_ability_min=0.0, ego_ability_max=0.1,
            driving_style_bit_p=0.03, driving_style_license_p=0.25)

        scene = {"scene_id": "3f9a2c17d5e8", "x": 1200.0, "y": -800.0,
                 "region": "ls_downtown", "scene_seed": 987654321,
                 "env": {"weather": "CLEAR", "hour": 14, "minute": 30,
                         "vehicle": "blista",
                         "density_vehicle": 2.5, "density_ped": 3.5,
                         "density_parked": 1.5,
                         "seed_counts": {"vehicles": 10, "peds": 12, "cyclists": 3}},
                 "scenario": "ego_into_cross_traffic", "n_variations": 2}
        plan = [
            ({"name": "both_sane", "ego_chaos": 0.0, "actor_chaos": 0.0}, calm),
            ({"name": "both_chaotic", "ego_chaos": 1.0, "actor_chaos": 1.0}, chaos),
        ]

        class NoIOWriter(writermod.ClipWriter):
            def add_frame(self, image_bgr, pose, extra=None):
                import json
                row = {"i": self.n, "game_time_ms": pose["game_time_ms"],
                       "position": pose["position"], "theta_deg": pose["theta_deg"],
                       "fov_deg": pose["fov_deg"]}
                if extra:
                    row.update(extra)
                self._pose_fh.write(json.dumps(row) + "\n")
                self._update_quality(image_bgr, pose, self.n)
                if self.first_pose is None:
                    self.first_pose = pose
                self.last_pose = pose
                self.n += 1
                return self.n - 1
        episode.ClipWriter = NoIOWriter

        # ONE plugin across both clips, so plugin.messages holds both Configs and
        # both SetActorBehaviours in order. One runner per variation: the config
        # IS the variation, and building the runner around it keeps this test
        # independent of how run_generator hands a per-variation config over.
        plugin = FakePlugin(warm_frames=5)
        rng = random.Random(7)
        metas = []
        for var, cfg in plan:
            runner = episode.EpisodeRunner(plugin, cfg, rng)
            metas.append(runner.run_clip(scene["x"], scene["y"], scene["region"], out,
                                         scene=scene, variation=var))
        a, b = metas

        # (1) The same staged incident -- the one the scene names.
        assert a["scenario"]["name"] == b["scenario"]["name"], \
            "variations staged different scenarios: %s vs %s" % (
                a["scenario"]["name"], b["scenario"]["name"])
        assert a["scenario"]["name"] == scene["scenario"], \
            "scenario %r is not the scene's %r" % (a["scenario"]["name"], scene["scenario"])

        # (2) The same place: both Config messages relocate to the scene's (x, y).
        locs = [[float(v) for v in m.scenario.location[:2]]
                for m in plugin.messages if type(m).__name__ == "Config"]
        assert len(locs) == 2, "expected one Config per clip, saw %d" % len(locs)
        assert locs[0] == locs[1] == [scene["x"], scene["y"]], "Config locations: %s" % locs

        # (3) The same conditions: what the scene pins, both clips report.
        for m in metas:
            env = m["environment"]
            for k in ("weather", "hour", "minute", "vehicle", "seed_counts"):
                assert env[k] == scene["env"][k], \
                    "%s: clip %r has %r, scene has %r" % (k, m["clip_id"], env[k], scene["env"][k])

        # (4) Different behaviour: one SetActorBehaviour per clip, and they differ.
        beh = [dict(m.__dict__) for m in plugin.messages
               if type(m).__name__ == "SetActorBehaviour"]
        assert len(beh) == 2, "expected one SetActorBehaviour per clip, saw %d" % len(beh)
        assert beh[0] != beh[1], "both clips sent the same actor behaviour: %s" % beh[0]
        assert beh[0]["drivingStyle"] == 786603, beh[0]
        assert beh[1]["drivingStyle"] == 512 | 262144, beh[1]

        # (5) Naming and provenance: "<scene_id>-<name>", and a scene block on disk.
        for m, (var, _cfg) in zip(metas, plan):
            want = "%s-%s" % (scene["scene_id"], var["name"])
            assert m["clip_id"] == want, "clip_id %r, expected %r" % (m["clip_id"], want)
            with open(os.path.join(out, "clips", m["clip_id"], "meta.json")) as f:
                on_disk = _json.load(f)
            sb = on_disk.get("scene")
            assert sb, "%s: meta.json has no scene block" % m["clip_id"]
            assert sb["scene_id"] == scene["scene_id"], sb
            assert sb["scene_seed"] == scene["scene_seed"], sb
            assert sb["n_variations"] == scene["n_variations"], sb
            assert sb["variation"]["name"] == var["name"], sb
            assert sb["variation"]["ego_chaos"] == var["ego_chaos"], sb
            assert sb["variation"]["actor_chaos"] == var["actor_chaos"], sb
            assert on_disk["environment"]["ego_chaos"] == var["ego_chaos"], on_disk["environment"]
            assert on_disk["environment"]["actor_chaos"] == var["actor_chaos"], on_disk["environment"]

        print("  scene %s: %s x2 at %s, actors %d vs %d, clips %s"
              % (scene["scene_id"], a["scenario"]["name"], locs[0],
                 beh[0]["drivingStyle"], beh[1]["drivingStyle"],
                 [m["clip_id"] for m in metas]))
    finally:
        _shutil.rmtree(out, ignore_errors=True)


print("variations-share-scene test passed.")

def test_rockstar_clip_recording():
    """With record_clip on, the client must drive the Editor's recorder around
    the recorded window and claim the file it wrote.

    The contract: SetCapturePause(false) is the generator's job (tested by
    inspection there); per clip, exactly one "start" at the first recorded frame,
    then "save" for a kept clip or "discard" for a rejected one; the new .clip in
    the library (which the Editor names itself) is moved into the clip folder as
    clip.clip with its thumbnail, and meta.json says so.
    """
    import json as _json, shutil as _shutil
    import config as cfgmod, coverage, episode

    out = tempfile.mkdtemp(prefix="rclip_")
    lib = os.path.join(out, "library")
    os.makedirs(lib)
    try:
        cfg = cfgmod.CaptureConfig(width=64, height=36,
                                   clip_seconds_min=6.0, clip_seconds_max=6.0,
                                   discard_lead_s=2.0,
                                   warmup_seconds_min=0.0, warmup_seconds_max=1.0,
                                   warmup_min_speed=0.0, out_dir=out,
                                   image_format="png",
                                   record_clip=True, clip_library_dir=lib,
                                   clip_save_wait_s=3.0)

        class EditorPlugin(FakePlugin):
            """FakePlugin that behaves like the Rockstar Editor's recorder: reports
            ClipRecording while recording and writes a named file on save."""

            def __init__(self, *a, **kw):
                FakePlugin.__init__(self, *a, **kw)
                self.recording = False
                self.saved = 0

            def sendMessage(self, m):
                if type(m).__name__ == "SetClipRecording":
                    if m.action == "start":
                        self.recording = True
                    elif m.action == "discard":
                        # Manual mode: STOP alone drops the buffer. Nothing written.
                        self.recording = False
                    elif m.action == "save":
                        if self.recording:
                            self.saved += 1
                            name = "Sep-15-2026-Clip-%04d" % self.saved
                            with open(os.path.join(lib, name + ".clip"), "wb") as f:
                                f.write(b"\0" * 4096)
                            with open(os.path.join(lib, name + ".jpg"), "wb") as f:
                                f.write(b"\xff\xd8" + b"\0" * 64)
                        self.recording = False
                return FakePlugin.sendMessage(self, m)

            def recvMessage(self):
                m = FakePlugin.recvMessage(self)
                m["ClipRecording"] = self.recording
                return m

        plugin = EditorPlugin(warm_frames=5)
        runner = episode.EpisodeRunner(plugin, cfg, random.Random(5))
        sampler = coverage.CoverageSampler(os.path.join(out, "rc.json"), seed=5)
        x, y, region = sampler.propose()
        meta = runner.run_clip(x, y, region, out)
        assert meta["keep"], meta["reject_reasons"]
        acts = [m.action for m in plugin.messages if type(m).__name__ == "SetClipRecording"]
        assert acts == ["start", "save"], acts
        d = os.path.join(out, "clips", meta["clip_id"])
        assert os.path.exists(os.path.join(d, "clip.clip")), os.listdir(d)
        assert os.path.exists(os.path.join(d, "clip_thumb.jpg"))
        assert not os.listdir(lib), "library should be empty after the move: %r" % os.listdir(lib)
        rc = meta["rockstar_clip"]
        assert rc["saved"] is True and rc["file"] == "clip.clip"
        assert rc["original_name"] == "Sep-15-2026-Clip-0001.clip", rc
        assert rc["recorder_confirmed"] is True
        assert rc["recorded_s"] >= 3.5, rc
        on_disk = _json.load(open(os.path.join(d, "meta.json")))
        assert on_disk["rockstar_clip"]["saved"] is True

        # The .clip origin is the first frame the recorder reported running, which
        # is at or just after the first recorded frame -- never before it.
        assert 0 <= rc["offset_ms"] < 1500, rc["offset_ms"]

        # A rejected clip discards instead of saving, and nothing is written.
        # (fade_at must land INSIDE the recorded window and past the Editor's 3 s
        # minimum -- lead 2 s at 20 Hz is frame 40, 3.5 s more is frame 110 -- or
        # the recorder never starts / nothing is written and the delete path is
        # never exercised.)
        crashed = EditorPlugin(warm_frames=5, fade_at=140)
        runner2 = episode.EpisodeRunner(crashed, cfg, random.Random(6))
        x2, y2, region2 = sampler.propose()
        meta2 = runner2.run_clip(x2, y2, region2, out)
        assert not meta2["keep"]
        acts2 = [m.action for m in crashed.messages if type(m).__name__ == "SetClipRecording"]
        assert acts2 == ["start", "discard"], acts2
        rc2 = meta2["rockstar_clip"]
        assert rc2["saved"] is False and "discarded" in rc2.get("reason", ""), rc2
        assert not os.listdir(lib), "nothing may be left in the library: %r" % os.listdir(lib)
        assert not os.path.exists(os.path.join(out, "clips", meta2["clip_id"], "clip.clip"))

        print("rockstar-clip recording test passed.")
        print("  kept clip: %s -> clip.clip (%d B), offset %d ms, recorded %.1f s; "
              "rejected clip discarded, library empty"
              % (rc["original_name"], rc["bytes"], rc["offset_ms"], rc["recorded_s"]))
    finally:
        _shutil.rmtree(out, ignore_errors=True)


def test_frames_off():
    """capture_frames=False: the plugin sends no image, and the clip must still
    come out whole -- every pose row present with file=None, no frame files,
    the clip kept on its pose-based gates alone."""
    import json as _json, shutil as _shutil
    import config as cfgmod, coverage, episode

    out = tempfile.mkdtemp(prefix="noframes_")
    try:
        cfg = cfgmod.CaptureConfig(width=64, height=36,
                                   clip_seconds_min=6.0, clip_seconds_max=6.0,
                                   discard_lead_s=2.0,
                                   warmup_seconds_min=0.0, warmup_seconds_max=1.0,
                                   warmup_min_speed=0.0, out_dir=out,
                                   image_format="jpg", capture_frames=False)
        plugin = FakePlugin(warm_frames=5)        # its messages carry frame=None
        runner = episode.EpisodeRunner(plugin, cfg, random.Random(9))
        sampler = coverage.CoverageSampler(os.path.join(out, "nf.json"), seed=9)
        x, y, region = sampler.propose()
        meta = runner.run_clip(x, y, region, out)
        assert meta["keep"], meta["reject_reasons"]
        d = os.path.join(out, "clips", meta["clip_id"])
        rows = [_json.loads(l) for l in open(os.path.join(d, "poses.jsonl"))]
        assert len(rows) >= 50, len(rows)
        assert all(r.get("file") is None for r in rows), "frame-less rows must carry file=None"
        assert all("position" in r and "game_time_ms" in r for r in rows)
        frames_dir = os.path.join(d, "frames")
        assert not os.path.isdir(frames_dir) or not os.listdir(frames_dir), "no frame files expected"
        # The Config that relocates the ego must ask the plugin for no frames.
        cfgs = [m for m in plugin.messages if type(m).__name__ == "Config"]
        assert cfgs and all(getattr(c.dataset, "captureFrames", True) is False for c in cfgs), \
            "Config.dataset.captureFrames must be False"
        print("frames-off test passed.")
        print("  %d pose rows, no images, kept on pose gates" % len(rows))
    finally:
        _shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    # ⚠ Kept OUT of module scope on purpose: anything importing this file for
    # FakePlugin (the determinism review does) must not execute the test suite
    # as a side effect of the import.
    test_discard_lead()
    test_variations_share_scene()
    test_rockstar_clip_recording()
    test_frames_off()
