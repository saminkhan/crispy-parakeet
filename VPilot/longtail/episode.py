"""One clip = one episode: relocate, gate, record, orchestrate, label, tear down.

Two invariants this runner never trades away:

  1. **No game-over state.** The plugin enforces this every frame
     (Scenario::enforceGameOverGuards) -- ped invincible, no arrest, no ragdoll,
     no ejection, no restart, no fade. The runner additionally *aborts* a clip if
     a fade or an out-of-vehicle ped is ever observed, so a guard failure shows
     up as a dropped clip rather than as corrupt training data.
  2. **The POV stays on the front of the ego vehicle.** Enforced plugin-side by
     Scenario::enforceEgoIntegrity, which re-seats the ped and rebuilds the
     scripted camera if either is lost.

★ Warmup is GATE-based, not timer-based. A fixed warmup is a fixed tax on every
clip; at 5 s clips a 20 s warmup means 80% of the run is warmup. The three things
it used to buy are handled directly: ego speed is set at spawn, collision is
pre-streamed during the previous clip, and event actors are spawned explicitly.
What is left -- visual LOD resolution -- is handled by the pop-in reject gate,
which is a variable cost on the fraction that fail instead of a fixed cost on all.

★ Counterfactual variations. run_clip() can capture one SCENE several times with
different behaviour: pass `scene=` (built once per location by build_scene()) and
`variation=`. The scene fixes location, weather, clock, ego vehicle, ambient
density, seeded population, WHICH scenario is staged and every random draw the
scenario makes; the variation moves only how the ego and the other road users
behave. Two RNG streams make that hold -- see run_clip() -- and neither is the
runner's own `rng`, which the single-clip path keeps using exactly as before.

⚠ What a variation reproduces is the setup, not the footage. GTA's ambient
traffic and its choice of random ped/vehicle models are the game's own
randomness and cannot be seeded from here, so two variations are never
frame-identical replays. Same place, same conditions, same ego vehicle, same
staged incident, same seeded-actor counts -- different traffic around them.
"""

import math
import random
import time
import uuid
import zlib

import numpy as np

from deepgtav.messages import (
    Config, Dataset, PrepareLocation, Scenario as GtaScenario,
    SetActorBehaviour, SetCameraPositionAndRotation, SetClockTime, SetSceneDensity,
    SetSurvivalMode, SeedScene, SetCameraMount, SetIgniteWrecks, SetRoadLeash,
    SetTimeScale, SetWeather, frame2numpy,
)

import drivingstyles as ds
import outcomes
import posemath
import scenarios as scen
from writer import ClipWriter


def _scenario_by_name(name):
    """The scenario class registered under `name` in scenarios.ALL_SCENARIOS.

    Scenes name their scenario as a string so a Scene dict survives JSON; this is
    the way back. Unknown names fail loudly -- a scene that silently fell back to
    a random scenario would capture N clips of N different incidents and call
    them variations of one.
    """
    for cls in scen.ALL_SCENARIOS:
        if cls.name == name:
            return cls
    raise KeyError("no scenario named %r; known: %s"
                   % (name, ", ".join(c.name for c in scen.ALL_SCENARIOS)))


def scene_rng(scene):
    """The SCENE stream: everything the staged incident draws."""
    return random.Random(int(scene["scene_seed"]))


def behaviour_rng(scene, variation):
    """The BEHAVIOUR stream for one variation of a scene: the ego's sample.

    Seeded from the scene AND the variation name, so the same variation of the
    same scene is reproducible, and two variations of one scene get different
    egos without either being able to disturb the scene stream.
    """
    name = str(variation.get("name", ""))
    return random.Random(int(scene["scene_seed"]) ^ zlib.crc32(name.encode("utf-8")))


class Ctx:
    """What a scenario is allowed to touch."""

    def __init__(self, client, cfg, rng):
        self._client = client
        self.cfg = cfg
        self.rng = rng
        self.traffic_models = scen.TRAFFIC_MODELS
        self.ego_speed_target = cfg.ego_cruise_speed
        self._last_pose = None
        self._last_state = None
        self._next_actor = 0

    def send(self, message):
        self._client.sendMessage(message)

    def new_actor_id(self):
        self._next_actor += 1
        return self._next_actor

    def ego_position(self):
        if self._last_pose is None:
            return (0.0, 0.0, 0.0)
        return tuple(self._last_pose["position"])

    def ego_speed(self):
        """Measured speed, falling back to the target if we have no reading yet.

        Scenarios solve spawn geometry from this, so it must be the real value --
        using the requested cruise speed would put every actor at the wrong TTC.
        """
        if self._last_state and self._last_state.get("speed", 0.0) > 0.5:
            return float(self._last_state["speed"])
        return float(self.ego_speed_target)

    def sample_ttc(self):
        return self.rng.uniform(self.cfg.ttc_min, self.cfg.ttc_max)


class EpisodeRunner:
    def __init__(self, client, cfg, rng, bias=None):
        self._imgbuf = None   # reused frame buffer; see _recv
        # Where the previous clip left the ego, so the next relocate can be
        # detected from its very first frame. None before the first clip.
        self._last_clip_xy = None
        # Last ScenarioGen seen; captured before each Config so warmup can wait for
        # the plugin to actually rebuild rather than for the ego to look nearby.
        self._last_gen = None
        self.client = client
        self.cfg = cfg
        self.rng = rng
        self.bias = bias
        self.labeler = outcomes.OutcomeLabeler()

    # ------------------------------------------------------------------
    def _recv(self):
        """Receive one message -> (image_bgr, pose, ego_state) or None."""
        msg = self.client.recvMessage()
        if msg is None or "CameraPosition" not in msg:
            return None
        pose = posemath.pose_from_message(msg)
        state = posemath.ego_state_from_message(msg)
        img = None
        if msg.get("frame"):
            # ⚠ frame2numpy allocates a fresh HxWx3 array every frame -- 6.2 MB at
            # 1080p, ~155 MB/s of churn at capture rate. Python does not return
            # that to the OS promptly, so the client's RSS climbed to ~2.6 GB for
            # what is genuinely one frame of working set. Decode into a buffer we
            # own and reuse: the writer encodes synchronously and retains only a
            # downscaled copy, so nothing outlives the call.
            decoded = frame2numpy(msg["frame"], (self.cfg.width, self.cfg.height))
            if self._imgbuf is None or self._imgbuf.shape != decoded.shape:
                self._imgbuf = np.empty_like(decoded)
            np.copyto(self._imgbuf, decoded)
            del decoded
            img = self._imgbuf
        return img, pose, state

    # ------------------------------------------------------------------
    def _sample_environment(self):
        """The single-clip environment sample, SCENE and EGO knobs interleaved.

        ⚠ The draw ORDER here is part of the recorded seed's contract: a run
        pins its seed in capture_config.json precisely so its clips can be
        replayed later, and reordering these calls changes every clip a seed
        produces. sample_scene_env() / sample_ego_behaviour() below draw the same
        quantities split by group for the scene path; this function is not
        rewritten in terms of them because doing so would reorder the draws.
        """
        cfg, rng = self.cfg, self.rng
        if cfg.sample_driving_style_bits:
            # ⚠ p_license was not passed, so it silently kept sample()'s default of
            # 0.25 no matter what the config said -- the licence bits did not move
            # with the chaos dial at all.
            style, style_bits = ds.sample(
                rng, p_restraint=cfg.driving_style_bit_p,
                p_license=getattr(cfg, "driving_style_license_p", 0.25))
        else:
            style, style_bits = ds.NORMAL, ["NORMAL"]
        return {
            "weather": rng.choice(cfg.weathers),
            "hour": rng.choice(cfg.hours),
            "minute": rng.randrange(60),
            "vehicle": rng.choice(cfg.ego_vehicles),
            "speed": rng.uniform(cfg.ego_speed_min, cfg.ego_speed_max),
            "style": style,
            "style_bits": style_bits,
            "restraint": round(ds.restraint_score(style), 3),
            "aggressiveness": rng.uniform(cfg.ego_aggressiveness_min, cfg.ego_aggressiveness_max),
            "ability": rng.uniform(cfg.ego_ability_min, cfg.ego_ability_max),
            "density_vehicle": rng.uniform(cfg.density_vehicle_min, cfg.density_vehicle_max),
            "density_ped": rng.uniform(cfg.density_ped_min, cfg.density_ped_max),
            "density_parked": rng.uniform(cfg.density_parked_min, cfg.density_parked_max),
            # Population is part of the SETUP, so it varies per clip like weather
            # and vehicle do. A constant count makes every scene the same shape.
            "seed_counts": {
                "vehicles": rng.randint(cfg.seed_vehicles_min, cfg.seed_vehicles_max),
                "peds": rng.randint(cfg.seed_peds_min, cfg.seed_peds_max),
                "cyclists": rng.randint(cfg.seed_cyclists_min, cfg.seed_cyclists_max),
            },
        }

    def sample_scene_env(self, rng):
        """The SCENE half of the environment: what one scene's variations share.

        Drawn from the runner's own config on purpose -- the SCENE knobs (density
        and population ranges) come from the top-level chaos and are the same in
        every variation's config, so there is exactly one answer.
        """
        cfg = self.cfg
        return {
            "weather": rng.choice(cfg.weathers),
            "hour": rng.choice(cfg.hours),
            "minute": rng.randrange(60),
            "vehicle": rng.choice(cfg.ego_vehicles),
            "density_vehicle": rng.uniform(cfg.density_vehicle_min, cfg.density_vehicle_max),
            "density_ped": rng.uniform(cfg.density_ped_min, cfg.density_ped_max),
            "density_parked": rng.uniform(cfg.density_parked_min, cfg.density_parked_max),
            "seed_counts": {
                "vehicles": rng.randint(cfg.seed_vehicles_min, cfg.seed_vehicles_max),
                "peds": rng.randint(cfg.seed_peds_min, cfg.seed_peds_max),
                "cyclists": rng.randint(cfg.seed_cyclists_min, cfg.seed_cyclists_max),
            },
        }

    @staticmethod
    def sample_ego_behaviour(rng, cfg):
        """The EGO half: driving style, speed, aggressiveness, ability.

        `cfg` is the VARIATION's config, not the runner's -- these ranges are what
        ego_chaos moves. `rng` is the behaviour stream for that variation.
        """
        if cfg.sample_driving_style_bits:
            style, style_bits = ds.sample(
                rng, p_restraint=cfg.driving_style_bit_p,
                p_license=getattr(cfg, "driving_style_license_p", 0.25))
        else:
            style, style_bits = ds.NORMAL, ["NORMAL"]
        return {
            "speed": rng.uniform(cfg.ego_speed_min, cfg.ego_speed_max),
            "style": style,
            "style_bits": style_bits,
            "restraint": round(ds.restraint_score(style), 3),
            "aggressiveness": rng.uniform(cfg.ego_aggressiveness_min, cfg.ego_aggressiveness_max),
            "ability": rng.uniform(cfg.ego_ability_min, cfg.ego_ability_max),
        }

    def build_scene(self, x, y, region, rng, n_variations):
        """Everything one location's variations will share, decided once.

        Returns the Scene dict run_clip(scene=...) consumes:
            scene_id (12 hex), x, y, region, scene_seed, env (the SCENE half of
            the environment), scenario (name), n_variations.

        `rng` is the run's scene stream -- in a variation run the generator's own
        RNG only ever produces scene-level draws, because every behaviour draw
        comes from a per-variation stream. The scenario is picked here, before
        any clip has run, so it is picked WITHOUT the measured-ego-speed filter
        scen.pick() applies on the single-clip path: there is no measured speed
        yet, and the same incident has to be staged for every variation
        regardless of how fast each one's ego turns out to drive. Each variation
        still solves its spawn geometry from its own measured speed, so the TTC
        is honoured; a calm ego simply meets the actor closer in metres.
        """
        env = self.sample_scene_env(rng)
        scenario = scen.pick(rng, ego_speed=None, bias=self.bias)
        return {
            "scene_id": "%012x" % rng.getrandbits(48),
            "x": float(x), "y": float(y), "region": region,
            # 52 bits: distinct across any run length, and still exact in every
            # JSON reader (a double carries 53), since this is copied verbatim
            # into meta.json and scenes.jsonl and read back to reproduce a scene.
            "scene_seed": rng.getrandbits(52),
            "env": env,
            "scenario": scenario.name,
            "n_variations": int(n_variations),
        }

    def _relocate(self, x, y, env, cfg=None):
        """Config triggers Scenario::buildScenario, which road-snaps (x, y), sets
        the ego's forward speed directly, and blocks on collision streaming.

        `cfg` is the config in force for THIS clip -- the variation's when one is
        being captured, the runner's otherwise."""
        if cfg is None:
            cfg = self.cfg
        self.client.sendMessage(Config(
            scenario=GtaScenario(location=[x, y], time=[env["hour"], env["minute"]],
                                 weather=env["weather"], vehicle=env["vehicle"],
                                 drivingMode=[env["style"], env["speed"]],
                                 spawnedEntitiesDespawnSeconds=self._despawn_seconds(cfg)),
            dataset=Dataset(rate=cfg.rate_hz, speed=True, location=True, time=True,
                            frame=[cfg.width, cfg.height],
                            screenResolution=[cfg.screen_width, cfg.screen_height])))
        self.client.sendMessage(SetCameraMount(
            forwardFrac=cfg.cam_mount_forward_frac,
            upFrac=cfg.cam_mount_up_frac))
        self.client.sendMessage(SetCameraPositionAndRotation(
            x=cfg.cam_right, y=cfg.cam_forward, z=cfg.cam_up,
            rot_x=cfg.cam_rot_x, rot_y=cfg.cam_rot_y, rot_z=cfg.cam_rot_z))
        self.client.sendMessage(SetWeather(env["weather"]))
        self.client.sendMessage(SetClockTime(hour=env["hour"], minute=env["minute"], second=0))
        self.client.sendMessage(SetSceneDensity(
            vehicle=env["density_vehicle"], randomVehicle=env["density_vehicle"],
            parkedVehicle=env["density_parked"], ped=env["density_ped"],
            scenarioPed=env["density_ped"]))
        self.client.sendMessage(SetTimeScale(scale=1.0))
        # Re-sent per clip: Config rebuilds the scenario, so the leash's strike
        # counter and recovery flag have to start clean for the new ego.
        self.client.sendMessage(SetRoadLeash(
            enabled=cfg.road_leash, dist=cfg.road_leash_dist_m,
            seconds=cfg.road_leash_seconds))
        self.client.sendMessage(SetIgniteWrecks(
            enabled=cfg.ignite_wrecks, belowHealth=cfg.ignite_below_health))
        # ★ How everyone ELSE drives, per clip. Sent unconditionally, not only
        # when a variation asks for it: the plugin keeps the last value it was
        # given, so after an actors_chaotic clip the next scene's both_sane clip
        # would otherwise inherit reckless traffic and the counterfactual would be
        # a lie. On the single-clip path the config defaults are the values the
        # plugin ships with, so this changes nothing there -- except that a JSON
        # override of an npc_* field now actually reaches the game.
        self.client.sendMessage(SetActorBehaviour(
            drivingStyle=cfg.npc_driving_style,
            aggressiveness=cfg.npc_aggressiveness,
            ability=cfg.npc_ability,
            cruiseSpeed=cfg.npc_cruise_speed,
            steersAround=cfg.npc_steers_around,
            trafficAggression=cfg.traffic_aggression,
            pedInteraction=cfg.ped_interaction,
            pedCrossChance=cfg.ped_cross_chance))

    def _despawn_seconds(self, cfg=None):
        """⚠ Upstream default is 60 s. Actors are spawned during warmup, so a long
        warmup plus a long clip can despawn them mid-clip. Set it explicitly with
        headroom rather than inheriting the default."""
        if cfg is None:
            cfg = self.cfg
        return float(cfg.warmup_seconds_max + cfg.clip_seconds_max
                     + getattr(cfg, "discard_lead_s", 0.0) + 30.0)

    def _apply_capture_guards(self, env):
        """Player survives, car does not, everyone reacts.

        Note aggressiveness/ability come from the per-clip sample: upstream pinned
        them at 0.0/100.0, a driver who never takes a risk and never errs.
        """
        self.client.sendMessage(SetSurvivalMode(
            policeIgnore=True, everyoneIgnore=False,
            playerInvincible=True, vehicleInvincible=False, seatbelt=True,
            aggressiveness=env["aggressiveness"], ability=env["ability"]))

    # ------------------------------------------------------------------
    def _warmup(self, ctx, target_xy=None, gen_before=None):
        """Gate on world state, not on a clock. Returns a diagnostics dict.

        ★ `arrived` is the gate that matters and it was the one missing. The other
        three -- world_ready, scene_streamed, speed -- are all satisfied by the
        PREVIOUS clip's state: the ego is still driving at the old location with
        collision loaded and speed up, so warmup exited in 0.6 s and recording
        began before the relocate had landed. The teleport then arrived ~3 s into
        the clip, and 3 of 7 clips shipped a 300 m - 2.4 km discontinuity between
        two adjacent frames. For world-model training data that is poison, and
        nothing downstream was looking for it.
        """
        cfg = self.cfg
        t0 = time.time()
        world_ready = scene_ready = False
        arrived = target_xy is None
        teleported = False
        quiet_frames = 0
        last_quiet_pos = None
        prev_warm_pos = None
        best_d = None
        speed = 0.0
        frames = 0
        radius = float(getattr(cfg, "warmup_arrive_radius_m", 200.0))
        # ⚠ Arrival gets its own, longer deadline. A cross-map relocate (observed:
        # sandy_shores, 2.3 km) can take longer than warmup_seconds_max to land,
        # and cutting warmup short there does not save time -- it produces a clip
        # recorded at the previous location, which is then thrown away whole.
        arrive_budget = float(getattr(cfg, "arrive_timeout_s", 30.0))
        while True:
            elapsed = time.time() - t0
            deadline = cfg.warmup_seconds_max if arrived else max(
                cfg.warmup_seconds_max, arrive_budget)
            if elapsed >= deadline:
                break
            got = self._recv()
            if got is None:
                continue
            frames += 1
            _img, pose, state = got
            ctx._last_pose, ctx._last_state = pose, state
            self._last_gen = state.get("scenario_gen")
            speed = state["speed"]
            world_ready = state["world_ready"] or world_ready
            scene_ready = state["scene_streamed"] or scene_ready
            if target_xy is not None and not arrived:
                pos = pose.get("position") or [0.0, 0.0, 0.0]
                d = math.hypot(pos[0] - target_xy[0], pos[1] - target_xy[1])
                if best_d is None or d < best_d:
                    best_d = d

                # ★ A radius around the REQUESTED point is the wrong test on its
                # own. The plugin road-snaps (x, y) to the nearest vehicle node, and
                # when the sampler proposes a point off the network -- a field, a
                # rooftop, water -- the snap lands hundreds of metres away. Measured
                # over 49 clips: 11 rejections for "never reached", of which 7 had
                # closest approach 242-660 m. Those relocates SUCCEEDED; the gate
                # was wrong, and it was throwing away 22% of all clips.
                #
                # The relocate IS a position discontinuity, so detect that directly:
                # it is exact, and independent of how far the snap moved us. The
                # radius stays as the fallback for when consecutive clips are close
                # enough that the jump is small.
                # ⚠ Two frames are needed to see a jump, and the relocate often
                # completes before the FIRST warmup frame arrives -- then there is
                # no jump to see. So also compare the first pose against where the
                # previous clip left the ego: same question, one frame earlier.
                if prev_warm_pos is None and self._last_clip_xy is not None:
                    if math.hypot(pos[0] - self._last_clip_xy[0],
                                  pos[1] - self._last_clip_xy[1]) > cfg.relocate_jump_min_m:
                        teleported = True
                if prev_warm_pos is not None:
                    if math.dist(prev_warm_pos, pos) > cfg.relocate_jump_min_m:
                        teleported = True
                prev_warm_pos = pos
                # ★ Exact, not inferred. ScenarioGen advances on every scenario
                # build, so `> gen_before` means the relocate HAS happened. The
                # proximity/teleport tests below stay only for the very first clip
                # of a run, when there is no prior generation to compare against.
                #
                # ⚠ Why proximity alone failed: for variations 2..N of a scene the
                # ego is already AT the scene, so "within radius" was true before
                # the relocate ran, warmup released in 0.6 s, and the relocate then
                # landed inside the clip (395 m at t=2.27). Worse, one variation
                # recorded 986 m from its siblings and passed every gate.
                gen_now = state.get("scenario_gen")
                if gen_before is not None and gen_now is not None:
                    if gen_now > gen_before and not arrived:
                        arrived = True
                        quiet_frames = 0
                elif teleported or d <= radius:
                    arrived = True
                    quiet_frames = 0
            # ★ Arrival is not enough: the position has to SETTLE. Observed with a
            # loose radius fallback -- warmup exited at its 0.6 s minimum because a
            # stale pre-relocate position happened to be inside the radius, and the
            # real teleport then landed 3-8 s INTO the clip. Three clips were lost
            # that way, one of them with the ego falling out of the world
            # (x pinned at 0.0, z -195 m, 49 m/s downward).
            #
            # Any jump after arrival means the relocate had not actually finished,
            # so the counter restarts.
            if arrived:
                if prev_warm_pos is not None and last_quiet_pos is not None:
                    if math.dist(last_quiet_pos, prev_warm_pos) > cfg.relocate_jump_min_m:
                        quiet_frames = 0
                    else:
                        quiet_frames += 1
                last_quiet_pos = prev_warm_pos

            if elapsed < cfg.warmup_seconds_min:
                continue
            if not arrived or quiet_frames < cfg.warmup_settle_frames:
                continue
            if cfg.require_world_ready and not world_ready:
                continue
            if cfg.require_scene_streamed and not scene_ready:
                continue
            if speed < cfg.warmup_min_speed:
                continue
            break
        return {"warmup_s": round(time.time() - t0, 3), "warmup_frames": frames,
                "world_ready": world_ready, "scene_streamed": scene_ready,
                "arrived": arrived,
                "settled_frames": quiet_frames,
                "arrived_via": ("teleport" if teleported else
                                ("radius" if arrived and target_xy is not None else None)),
                "closest_to_target_m": None if best_d is None else round(best_d, 1),
                "speed_at_t0": round(speed, 2),
                "timed_out": (time.time() - t0) >= cfg.warmup_seconds_max}

    # ------------------------------------------------------------------
    def run_clip(self, x, y, region, out_dir, next_xy=None,
                 scene=None, variation=None, gen_cfg=None, variation_index=None):
        """Capture one clip. Returns the meta dict written to meta.json.

        Single clip (scene is None): everything is drawn from the runner's own
        `rng`, exactly as it always was.

        One variation of a scene (scene given): `scene` is a build_scene() dict,
        `variation` is {"name", "ego_chaos", "actor_chaos"}, `gen_cfg` is the
        CaptureConfig merged for that variation (None means the runner's own
        config IS the variation's, which is how one runner per variation works),
        `variation_index` is its position in the plan, for the record only.
        """
        # The config in force for this clip. Only EGO/ACTOR knobs differ between
        # a variation's config and the runner's; SCENE knobs were already spent
        # when build_scene() sampled the environment.
        cfg = gen_cfg if gen_cfg is not None else self.cfg
        if scene is None:
            rng = self.rng
            clip_id = uuid.UUID(int=rng.getrandbits(128)).hex[:16]
            env = self._sample_environment()
        else:
            # ★ Two RNG streams, and they must not cross.
            #   SCENE stream     -- clip timing, the scenario's spawn geometry, its
            #                       TTC and trigger draws. Reseeded from scene_seed
            #                       for every variation, so the staged incident
            #                       is drawn identically each time.
            #   BEHAVIOUR stream -- the ego's style / speed / aggressiveness /
            #                       ability. Seeded per (scene, variation).
            # If both came from one Random, a variation whose ego sample took a
            # different number of draws (bit-sampled styles do) would shift every
            # scenario draw after it, and "the same scene" would not be the same.
            # Neither touches self.rng, which stays the single-clip stream.
            rng = scene_rng(scene)
            ego_rng = behaviour_rng(scene, variation or {})
            env = dict(scene["env"])
            env["seed_counts"] = dict(scene["env"]["seed_counts"])
            env.update(self.sample_ego_behaviour(ego_rng, cfg))
            clip_id = "%s-%s" % (scene["scene_id"], (variation or {}).get("name"))
        # Recorded on both paths so the environment block has one shape; null
        # means "no chaos split -- a single-dial run".
        env["ego_chaos"] = None if variation is None else variation.get("ego_chaos")
        env["actor_chaos"] = None if variation is None else variation.get("actor_chaos")

        duration = rng.uniform(cfg.clip_seconds_min, cfg.clip_seconds_max)
        # [longtail] Capture a lead-in that is thrown away. The world streams in for
        # ~2s after a teleport and that is where essentially all pop-in occurs; the
        # lead absorbs it so the written clip is clean.
        lead = float(getattr(cfg, "discard_lead_s", 0.0))
        # Clear-and-reseed is heavier than reseed alone; the first frames after it
        # were still resolving LOD (8 pop-in events on a both_chaotic variation).
        if getattr(cfg, "deterministic_population", False):
            lead += 1.0
        total = lead + duration
        # Trigger fraction applies to the DELIVERED clip, not the lead.
        trigger_at = lead + duration * rng.uniform(cfg.trigger_fraction_min,
                                                   cfg.trigger_fraction_max)
        slowmo = rng.random() < cfg.slowmo_probability

        gen_before = self._last_gen
        self._relocate(x, y, env, cfg)
        # ctx.rng is the scene stream on the scene path: every draw a scenario
        # makes goes through it. ctx.cfg is the VARIATION's config, so
        # ctx.sample_ttc() maps the same scene-stream quantile onto that
        # variation's [ttc_min, ttc_max] -- same draw, actor-chaos-dependent range,
        # which is exactly what "the TTC range varies, the TTC sample does not"
        # means in practice (rng.uniform(a, b) is a + (b - a) * random()).
        ctx = Ctx(self.client, cfg, rng)
        ctx.ego_speed_target = env["speed"]

        # ⚠ Same-location relocates. Variations 2..N of a scene teleport the ego
        # back to where variation 1 started, a few hundred metres from where it
        # ended. _warmup()'s jump detector may not fire on a move that short, so
        # arrival falls to the radius fallback plus the settle count -- and a
        # teleport that lands after the settle count is caught by the in-clip
        # continuity backstop as a rejected clip, never as bad data.
        warm = self._warmup(ctx, target_xy=(x, y), gen_before=gen_before)

        # [longtail] Seed AFTER warmup: the ego has to be settled at its final
        # location first, or the traffic is placed around where it used to be.
        # Before the record loop, because placing ~18 entities blocks the script
        # thread for a second or two while models stream.
        seed = env["seed_counts"]
        if any(seed.values()):
            self.client.sendMessage(SeedScene(
                vehicles=seed["vehicles"], peds=seed["peds"],
                cyclists=seed["cyclists"], radius=cfg.seed_radius_m,
                # The scene seed when replaying a scene, else a per-clip draw -- so
                # a single-clip run is reproducible from its own seed too.
                seed=(scene["scene_seed"] if scene else rng.getrandbits(32)),
                clearAmbient=bool(getattr(cfg, "deterministic_population", False))))

        if scene is None:
            scenario = scen.pick(rng, ego_speed=ctx.ego_speed(), bias=self.bias)
        else:
            # The scene decided; no draw is spent here, so the scene stream is
            # positioned identically for setup() in every variation.
            scenario = _scenario_by_name(scene["scenario"])()
        setup_info = scenario.setup(ctx) or {}
        self._apply_capture_guards(env)

        # ---------------- RECORD ---------------------------------------
        writer = ClipWriter(out_dir, clip_id, cfg)
        ego_frames = []
        t0_game = last_game = None
        out_of_order = 0
        triggered = slowmo_on = slowmo_done = prepared = False
        trigger_info, trigger_frame = {}, None
        abort_reason = None
        last_moving_t = 0.0
        had_collision = False
        rolled_over = False
        roll_started_t = None
        end_reason = None
        # [longtail] road containment. Counted over delivered frames only: the
        # lead is discarded, and the ego is still road-snapping during it.
        offroad_frames = inclip_frames = 0
        last_onroad_t = 0.0
        road_dists = []
        prev_pos = prev_t = None
        delivered_span = 0.0
        max_jump_m = 0.0

        while True:
            got = self._recv()
            if got is None:
                continue
            img, pose, state = got
            ctx._last_pose, ctx._last_state = pose, state

            # --- game-over backstop -----------------------------------
            # The plugin should make these impossible. If one is ever observed,
            # the clip is discarded rather than written -- a guard regression must
            # surface as a dropped clip, never as a black frame in the data.
            if state["screen_faded"]:
                abort_reason = "screen faded (game-over guard failed)"
                break
            if not state["ped_in_vehicle"]:
                abort_reason = "ego ped left the vehicle (POV integrity failed)"
                break

            gt = state["game_time_ms"]
            if t0_game is None:
                t0_game, last_game = gt, gt - 1
            if gt <= last_game:
                out_of_order += 1
                continue
            last_game = gt
            t = (gt - t0_game) / 1000.0

            if state.get("speed", 0.0) >= self.cfg.stuck_speed_mps:
                last_moving_t = t
            # ⚠ A lawful ego STOPS at red lights. The stuck detector read a
            # both_sane variation waiting at a junction as "immobile 4 s before any
            # impact" and rejected it -- sane driving being rejected for being sane.
            if state.get("at_traffic_light"):
                last_moving_t = t

            # Everything before the lead is captured (so the world settles and the
            # ego reaches speed) but never written.
            in_clip = t >= lead

            if state.get("collided"):
                had_collision = True

            # ⚠⚠ This aborted on `on_roof` unconditionally, so a ROLLOVER could never
            # be labelled: outcomes.py has a rollover rule that the clip never
            # reached, and 95 clips produced exactly zero of them. A car that
            # flips after an impact is one of the events this generator exists to
            # capture, not a broken clip.
            #
            # This is the third guard to make the same mistake -- the stuck detector
            # and the travel-distance gate both discarded post-collision clips for
            # the same reason. The distinction is always the same one: BEFORE an
            # impact it is a bad spawn, AFTER an impact it is the payload.
            #
            # An overturn with no collision is still a dead clip: a car on its side
            # in a ditch records 15 s of static scenery and passes every other gate.
            if state.get("on_roof"):
                if not had_collision:
                    abort_reason = "ego overturned with no impact (bad spawn)"
                    break
                rolled_over = True
                # Give the roll time to play out, then stop: the informative part is
                # the flip and the slide, not the minutes of lying still afterwards.
                if roll_started_t is None:
                    roll_started_t = t
                elif t - roll_started_t > cfg.rollover_tail_s:
                    end_reason = "rollover settled"
                    break

            # ⚠ Once the ego has actually collided, coming to rest is the EXPECTED
            # end of the event, not a failure. Rejecting those threw away exactly
            # the long-tail clips this generator exists to capture (observed: a
            # 14.7s major_collision discarded because the wreck stopped moving).
            if t > self.cfg.stuck_grace_s and not had_collision:
                stuck_for = t - last_moving_t
                # ⚠ 4 s is calibrated for an ego that never stops. A LAWFUL ego stops
                # -- at lights (exempted above), but also behind traffic, at stop
                # signs, yielding at junctions -- and a both_sane variation was
                # rejected for exactly that, 11 s into a good clip. A restrained
                # style gets a stationary window long enough for ordinary driving
                # while a genuinely wedged spawn is still caught.
                stuck_limit = self.cfg.stuck_seconds
                if getattr(cfg, "driving_style_bit_p", 0.0) >= 0.5:
                    stuck_limit = max(stuck_limit, getattr(cfg, "stuck_seconds_lawful", 14.0))
                if stuck_for > stuck_limit:
                    abort_reason = ("ego immobile %.1fs before any impact "
                                    "(speed < %.1f m/s)"
                                    % (stuck_for, self.cfg.stuck_speed_mps))
                    break
            # [longtail] ★ Continuity backstop. Whatever the cause, a clip must
            # never contain a teleport: two adjacent frames 2 km apart destroy the
            # clip for anything that learns dynamics, and every other signal in the
            # pipeline reads healthy across the seam. The arrival gate in _warmup()
            # prevents the known cause (recording starting before the relocate
            # landed); this catches the ones we have not thought of.
            pos_now = pose.get("position") or [0.0, 0.0, 0.0]
            if prev_pos is not None:
                jump = math.dist(prev_pos, pos_now)
                dt_frame = max(1e-3, t - prev_t)
                max_jump_m = max(max_jump_m, jump)
                # Allow for a genuinely fast car plus a long frame gap before
                # calling it a teleport: 60 m/s is well above anything in the
                # vehicle list, and the floor covers sub-frame jitter.
                if jump > max(cfg.max_jump_m, 60.0 * dt_frame):
                    abort_reason = ("position discontinuity %.0f m in %.2fs at "
                                    "t=%.2f (teleport inside the clip)"
                                    % (jump, dt_frame, t))
                    break
            prev_pos, prev_t = pos_now, t

            # [longtail] Off-road accounting. The plugin leash is the actuator;
            # this is the measurement and the backstop for when it fails (no node
            # within reach to steer to, or the car wedged against geometry).
            road_dist = float(state.get("road_node_dist") or 0.0)
            if road_dist <= cfg.offroad_dist_m:
                last_onroad_t = t
            elif t - last_onroad_t > cfg.offroad_abort_s:
                abort_reason = ("ego off-road %.1fs continuously (%.0f m from the "
                                "node network; leash did not recover)"
                                % (t - last_onroad_t, road_dist))
                break

            if in_clip:
                delivered_span = t - lead
                inclip_frames += 1
                road_dists.append(road_dist)
                if road_dist > cfg.offroad_dist_m:
                    offroad_frames += 1
                writer.add_frame(img, pose,
                                 extra={"t": round(t - lead, 4), "speed": state["speed"],
                                        "road_dist": round(road_dist, 2),
                                        # For the jitter question: a trajectory
                                        # sampled from the sim is smooth or it is
                                        # not, and the ego's own track is the
                                        # control. The id travels with it, or a
                                        # change of subject reads as a jump.
                                        "near_id": state.get("nearest_veh_id"),
                                        "near_pos": [round(v, 3) for v in
                                                     (state.get("nearest_veh_pos")
                                                      or [0.0, 0.0, 0.0])]})
                ego_frames.append(state)

            # --- slow-motion ramp around the trigger -------------------
            if slowmo and not slowmo_on and t >= max(0.0, trigger_at - cfg.slowmo_lead_s):
                self.client.sendMessage(SetTimeScale(scale=cfg.slowmo_scale))
                slowmo_on = True
            if slowmo_on and not slowmo_done and t >= trigger_at + cfg.slowmo_hold_s:
                self.client.sendMessage(SetTimeScale(scale=1.0))
                slowmo_done = True

            if not triggered and t >= trigger_at:
                trigger_info = scenario.trigger(ctx) or {}
                trigger_frame = writer.n - 1
                triggered = True
            elif triggered:
                scenario.tick(ctx, t)

            # --- pipeline the NEXT clip's streaming --------------------
            # Issued once, mid-clip, so the next relocate does not pay for it.
            if (cfg.pipeline_next_location and not prepared and next_xy
                    and t >= lead + duration * 0.5):
                self.client.sendMessage(PrepareLocation(x=next_xy[0], y=next_xy[1], z=0.0))
                prepared = True

            if t >= total:
                break

        if slowmo_on and not slowmo_done:
            self.client.sendMessage(SetTimeScale(scale=1.0))

        # ---------------- LABEL + VERDICT ------------------------------
        measured = self.labeler.measure(ego_frames)
        label = measured.pop("label")
        keep, reasons = writer.verdict(had_collision=had_collision)
        if abort_reason:
            keep, reasons = False, reasons + [abort_reason]

        # [longtail] Road containment verdict. An off-road clip is not a broken
        # clip -- every frame is valid, the ego is just driving through a field,
        # which is not the domain this generator exists to sample. It has to be
        # rejected explicitly or it ships looking healthy.
        offroad_fraction = (offroad_frames / float(inclip_frames)) if inclip_frames else 0.0
        road_dists.sort()
        road_dist_p95 = (road_dists[int(0.95 * (len(road_dists) - 1))]
                         if road_dists else 0.0)
        # A clip that never reached its requested location is not just suspect
        # footage -- it is filed against the wrong region, so it corrupts the
        # coverage ledger as well.
        if not warm.get("arrived", True):
            keep = False
            reasons = reasons + ["ego never reached the requested location "
                                 "(closest %s m)" % warm.get("closest_to_target_m")]

        # ⚠ Nothing checked delivered LENGTH. Observed: a 4.83 s clip kept silently
        # against a 15 s request. A clip ends early for a reason, and a dataset of
        # nominally-15 s clips that are sometimes 5 s is worse than one missing them.
        # ⚠ A rollover deliberately stops early once the flip has played out, so
        # the length gate must not treat that as a truncation. Only an UNPLANNED
        # short clip is a defect.
        if end_reason is None and delivered_span < duration * cfg.min_duration_fraction:
            keep = False
            reasons = reasons + ["clip ended early: %.1fs of a requested %.1fs"
                                 % (delivered_span, duration)]

        # ★ A variation that is not AT its scene is not a counterfactual of it. The
        # whole feature rests on this: two kept variations of one scene were found
        # 986 m apart, both passing every other gate.
        if scene is not None and writer.first_pose:
            fp = writer.first_pose.get("position") or [0.0, 0.0, 0.0]
            off = math.hypot(fp[0] - float(scene["x"]), fp[1] - float(scene["y"]))
            if off > getattr(cfg, "scene_max_offset_m", 150.0):
                keep = False
                reasons = reasons + ["variation started %.0f m from its scene "
                                     "(limit %.0f m)" % (off, cfg.scene_max_offset_m)]

        if offroad_fraction > cfg.offroad_max_fraction:
            keep = False
            reasons = reasons + ["ego off-road for %.0f%% of the clip (limit %.0f%%)"
                                 % (100.0 * offroad_fraction,
                                    100.0 * cfg.offroad_max_fraction)]

        road = ego_frames[-1] if ego_frames else {}
        last_pose = writer.last_pose if hasattr(writer, "last_pose") else None
        _lp = (last_pose or {}).get("position") if last_pose else None
        if _lp:
            self._last_clip_xy = (_lp[0], _lp[1])
        bb_w = int(road.get("backbuffer_w") or 0)
        bb_h = int(road.get("backbuffer_h") or 0)
        meta = writer.finalize({
            "clip_id": clip_id,
            "keep": keep,
            "reject_reasons": reasons,
            "outcome": {"label": label, "measurements": measured},
            "scenario": {"name": scenario.name, "setup": setup_info,
                         "trigger": trigger_info, "trigger_frame": trigger_frame,
                         "trigger_at_s": round(trigger_at, 3)},
            "environment": env,
            # Null on a single-clip run. The manifest reads scene_id and the
            # variation name from HERE, not from the directory name, because a
            # variation name may itself contain a hyphen.
            "scene": None if scene is None else {
                "scene_id": scene["scene_id"],
                "scene_seed": scene["scene_seed"],
                "variation": {"name": (variation or {}).get("name"),
                              "ego_chaos": (variation or {}).get("ego_chaos"),
                              "actor_chaos": (variation or {}).get("actor_chaos")},
                "n_variations": scene.get("n_variations"),
                "index": variation_index},
            "placement": {"proposed_xy": [x, y], "region": region,
                          "snapped_xy": (writer.first_pose or {}).get("position"),
                          "road_node_valid": road.get("road_node_valid"),
                          "road_node_density": road.get("road_node_density"),
                          "road_node_flags": road.get("road_node_flags")},
            # ★ Capture geometry, recorded so "were these frames really WxH?" is a
            # metadata query. `resampled` true means the renderer produced fewer
            # pixels than the frames claim and the intrinsics describe a grid that
            # does not exist -- see posemath's note on backbuffer_w.
            "capture": {"frame_wh": [cfg.width, cfg.height],
                        "backbuffer_wh": [bb_w, bb_h],
                        "resampled": bool(bb_w and (bb_w, bb_h) != (cfg.width, cfg.height)),
                        "discard_lead_s": lead,
                        "rate_hz": cfg.rate_hz,
                        "image_format": cfg.image_format},
            "ended": {"reason": end_reason or ("aborted" if abort_reason else "full length"),
                      "rolled_over": rolled_over,
                      "delivered_s": round(delivered_span, 2)},
            "continuity": {"max_jump_m": round(max_jump_m, 2),
                           "limit_m": cfg.max_jump_m},
            "containment": {"offroad_fraction": round(offroad_fraction, 4),
                            "offroad_frames": offroad_frames,
                            "road_dist_p95_m": round(road_dist_p95, 2),
                            "road_dist_max_m": round(road_dists[-1], 2) if road_dists else 0.0,
                            "offroad_dist_m": cfg.offroad_dist_m,
                            "leash_dist_m": cfg.road_leash_dist_m},
            "timing": dict(warm, requested_duration_s=round(duration, 3),
                           out_of_order_messages=out_of_order,
                           slowmo=slowmo,
                           slowmo_scale=cfg.slowmo_scale if slowmo else 1.0),
            "guards": {"everyoneIgnore": False, "vehicleInvincible": False,
                       "playerInvincible": True, "seatbelt": True,
                       "game_over_abort": abort_reason},
        })
        return meta
