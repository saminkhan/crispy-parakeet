"""Generator configuration. Everything tunable lives here or in a JSON override."""

import json
from dataclasses import dataclass, field, asdict


@dataclass
class CaptureConfig:
    # --- clip shape -----------------------------------------------------
    clip_seconds_min: float = 5.0
    clip_seconds_max: float = 30.0
    #: Seconds captured but NOT written, at the head of every clip. Measured:
    #: 96%% of pop-in events land in the first 12%% of a clip (0.17-1.76s), because
    #: the world is still streaming in after the teleport. Rejecting the whole clip
    #: threw away 13 good seconds to avoid 2 bad ones; discarding the lead keeps it.
    #: ⚠ Was 5.0, sized for a teleport that had not landed when recording began.
    #: The arrival gate in episode._warmup() now guarantees the relocate has landed
    #: and collision has streamed before the clip starts, so most of that lead was
    #: paying twice for the same thing -- 25% of every clip's frames captured and
    #: thrown away. 2 s still covers late LOD resolution, which the pop-in gate
    #: catches anyway.
    discard_lead_s: float = 2.0

    #: Where in the clip the orchestrated event fires, as a fraction of duration.
    #: Kept well inside the clip so there is context before AND after the event.
    trigger_fraction_min: float = 0.20
    trigger_fraction_max: float = 0.40

    # --- warmup (NOT recorded) ------------------------------------------
    # ★ Warmup is now GATE-based, not timer-based. A fixed warmup is a fixed tax
    # on every clip: at 5 s clips, a 20 s warmup means 80% of the run is warmup.
    # The three things it used to buy are handled directly instead:
    #   ego acceleration -> SET_VEHICLE_FORWARD_SPEED at spawn (instant)
    #   collision stream -> pipelined via PrepareLocation during the previous clip
    #   event actors     -> explicitly spawned, never waited for
    # What remains un-eliminable is visual LOD resolution, and that is handled by
    # the pop-in reject gate: a variable cost on the fraction that fail, rather
    # than a fixed cost on all of them.
    #: Small floor so the ego has physically moved before t=0.
    warmup_seconds_min: float = 0.6
    #: Hard cap so a bad spawn cannot stall the generator.
    warmup_seconds_max: float = 12.0
    #: Ego must reach this speed (m/s) before warmup can end. 0 disables the check.
    warmup_min_speed: float = 3.0
    #: Block until the plugin reports collision streamed in around the ego.
    #: ⚠ Do not disable. Without collision the car falls through the world.
    require_world_ready: bool = True
    #: Also wait for the async scene load to report complete. Costs a little more
    #: warmup, buys fewer pop-in rejections. Worth measuring both ways.
    require_scene_streamed: bool = False
    #: Pre-stream the next clip's location during the current one.
    pipeline_next_location: bool = True

    # --- image ----------------------------------------------------------
    width: int = 1920
    height: int = 1080
    screen_width: int = 1920
    screen_height: int = 1080
    image_format: str = "png"      # png = lossless; jpg for 5-10x less disk
    jpeg_quality: int = 95

    # --- capture rate ---------------------------------------------------
    #: Requested rate. ⚠ Advisory only -- real frame intervals are NOT uniform.
    #: Every frame carries its own GameTime, and that is what you should use.
    rate_hz: int = 20

    # --- video ------------------------------------------------------------
    #: Encode each kept clip to H.264 mp4 next to its frames.
    video: bool = True
    #: Drop the PNG frame dir after a successful encode. ⚠ The frames are the
    #: pose-aligned record; the mp4 is a convenience view. Only set this if you
    #: genuinely never want per-frame access again.
    #: ⚠ Storage, not quality, is the binding constraint on a long run. Measured:
    #: 251 MB of JPEG frames + 86 MB of mp4 per 15 s clip = 338 MB, so 1000 clips
    #: is 330 GB. Dropping the frames after encoding leaves 86 MB/clip -- 84 GB
    #: for 1000 -- and H.264 at CRF 16 is visually near-lossless.
    #:
    #: ⚠ It is still LOSSY and it adds INTER-frame compression. Decoded frames are
    #: not byte-identical to the JPEGs, and a model sensitive to per-frame texture
    #: statistics may see the difference. Set False when frames must be exact.
    video_only: bool = True
    video_crf: int = 16

    # --- run control ----------------------------------------------------
    out_dir: str = "./longtail_out"
    max_clips: int = 0             # 0 = run until stopped
    seed: int = 0

    # --- Rockstar Editor .clip recording ----------------------------------
    #: Record each clip with the game's own Rockstar Editor recorder as well. The
    #: .clip is the scene itself -- entity states the Editor replays in-engine --
    #: so it can be re-rendered later at another resolution or from another camera
    #: without capturing again. ★ Turning this on also turns the capture pause OFF
    #: (SetCapturePause false): the replay recorder latches on SET_GAME_PAUSED and
    #: saves nothing afterwards, while SET_TIME_SCALE(0) alone leaves it recording.
    record_clip: bool = False
    #: Capture images at all. Off = poses (+ .clip) only: the plugin never reads
    #: the backbuffer or freezes time, so game time runs at wall time and the run
    #: is ~3x faster. ⚠ With frames ON and record_clip on, the Editor's recorder
    #: sees ~3 s of rendering per second of game time and may chop the recording
    #: into 30 s segments; every segment is kept (clip.clip, clip.2.clip, ...).
    capture_frames: bool = True
    #: The Editor's library, where it writes clips (WSL path). "" = search the
    #: usual Documents locations. The runner fills this in from the registry.
    clip_library_dir: str = ""
    #: The Editor refuses to save anything shorter than 3 s. Discard rather than
    #: let a short abort fail with a toast on screen.
    clip_min_seconds: float = 3.5
    #: How long to wait for the saved .clip to appear in the library.
    clip_save_wait_s: float = 12.0

    # --- ego ------------------------------------------------------------
    ego_vehicles: list = field(default_factory=lambda: [
        "blista", "voltic", "packer", "sultan", "asea", "futo", "baller", "bison",
    ])
    ego_cruise_speed: float = 17.0
    ego_speed_min: float = 11.0
    ego_speed_max: float = 30.0

    #: ⚠ Upstream collapsed both of these to a driver who never takes a risk
    #: (aggressiveness 0.0) and never makes a mistake (ability 100.0). Inverting
    #: them is the cheapest long-tail source in the system and needs no actors.
    #: Note ability is 0-1 here and passed through as-is.
    ego_aggressiveness_min: float = 0.4
    ego_aggressiveness_max: float = 1.0
    ego_ability_min: float = 0.05
    ego_ability_max: float = 0.7

    #: Sample the driving-style bitmask bit-by-bit rather than from ~5 presets.
    #: ~12 independent bits gives a smooth gradient between "obeys everything"
    #: and "ignores everything" instead of five discrete behaviours.
    sample_driving_style_bits: bool = True
    driving_style_bit_p: float = 0.35     # P(bit set) for the "restraint" bits
    #: ⚠ driving_style_bit_p only scales the eight RESTRAINT bits. The licence bits
    #: (ALLOW_WRONG_WAY, TAKE_SHORTEST_PATH) kept drivingstyles.sample()'s own
    #: default of 0.25 at every setting, so a "calm" run still put the ego on the
    #: wrong side of the road in a quarter of clips -- measured over 20k samples:
    #: 25.2% wrong-way at chaos 0.0, identical to chaos 1.0.
    driving_style_license_p: float = 0.25

    # --- road containment -------------------------------------------------
    #: ★ Chaos has to happen *on the road*. Two things take the ego off it: a
    #: driving style carrying IGNORE_ROADS/IGNORE_ALL_PATHING (see
    #: `drivingstyles.OFFROAD_BITS` -- never sampled now, and masked again
    #: plugin-side), and a collision that knocks the car onto the verge. The
    #: first is prevented; the second is recovered from by the plugin's road
    #: leash, and whatever survives both is measured and gated here.
    #:
    #: ⚠ Distances are to the nearest *vehicle node*, which sits on the
    #: carriageway centreline. A legitimate outside lane of a wide road is
    #: already ~8 m out, so these thresholds are not "distance from tarmac".
    road_leash: bool = True
    road_leash_dist_m: float = 18.0       # plugin steers back beyond this
    road_leash_seconds: float = 1.5       # ...after this long, sustained
    offroad_dist_m: float = 22.0          # a frame counts as off-road beyond this
    #: Fraction of *delivered* frames allowed off-road before the clip is rejected.
    #: Not zero: a collision that puts the ego on the verge for the last second is
    #: exactly the footage we want, and the leash needs room to recover.
    offroad_max_fraction: float = 0.25
    #: Continuous seconds off-road before the clip is aborted outright. The leash
    #: gets ~2x its own reaction window before we conclude it has failed.
    offroad_abort_s: float = 5.0

    # --- continuity --------------------------------------------------------
    #: ★ The ego must be AT the requested location before the clip starts. Without
    #: this gate, warmup's other three conditions (world_ready / scene_streamed /
    #: speed) are all satisfied by the *previous* clip's state, so recording began
    #: at the old location and the teleport landed ~3 s into the clip. Observed in
    #: 3 of 7 clips as a 300 m - 2.4 km jump between two adjacent frames, with
    #: every other quality signal green.
    #: Fallback only, now that the relocate is detected as a discontinuity. Kept
    #: generous because the plugin road-snaps and the snap can be far from the
    #: request; it no longer has to be the primary test.
    warmup_arrive_radius_m: float = 800.0
    #: ★ The relocate IS a position discontinuity, so detect it directly rather
    #: than inferring it from proximity to the requested point. The plugin
    #: road-snaps (x, y), and a request that lands off the network snaps hundreds
    #: of metres away -- measured: 7 of 11 "never reached" rejections had closest
    #: approach 242-660 m on relocates that had actually succeeded.
    relocate_jump_min_m: float = 250.0
    #: ★ Consecutive quiet frames required AFTER arrival before the clip starts.
    #: Arrival alone is not enough: a stale pre-relocate position inside the radius
    #: released warmup at its 0.6 s minimum and the real teleport then landed 3-8 s
    #: INTO the clip. One of those had the ego falling out of the world. Any jump
    #: after arrival restarts the count.
    warmup_settle_frames: int = 20     # ~0.6 s at the observed sampling rate
    #: A clip shorter than this fraction of its requested duration is rejected
    #: rather than shipped. Observed: a 4.83 s clip kept silently against a 15 s
    #: request, because nothing checked delivered length.
    min_duration_fraction: float = 0.9
    #: Seconds of footage kept after the ego comes to rest on its roof. The flip
    #: and the slide are the informative part; lying still afterwards is not.
    rollover_tail_s: float = 4.0

    # --- scene seeding ------------------------------------------------------
    #: ★ Density, not driver aggression, was the binding constraint on chaos.
    #: Measured over 13 clips: mean 0.7 other vehicles damaged when <=4 were within
    #: 60 m, 3.3 when >=8 -- and the MEDIAN clip had only 4. ⚠ The density
    #: multipliers cannot fix it: they SCALE GTA's population model rather than
    #: inventing traffic, so 3x of a near-empty road is still near-empty. These
    #: place vehicles and VRUs outright.
    #: ⚠ Sampled per clip, not fixed. A constant 14/12/4 makes every scene the same
    #: shape: same traffic weight, same VRU exposure, same failure modes. The whole
    #: point of the generator is variety in the SETUP, and the population is part of
    #: the setup. A clip drawn at the low end is a quiet street; at the high end a
    #: crowded junction.
    seed_vehicles_min: int = 6
    seed_vehicles_max: int = 18
    #: VRUs: pedestrians, placed in a FORWARD cone 15-70 m ahead of the ego so they
    #: are actually encountered. Scattering them around the ego put them behind and
    #: beside the car where they never interact.
    seed_peds_min: int = 6
    seed_peds_max: int = 20
    #: VRUs: cyclists, placed on the node network.
    seed_cyclists_min: int = 2
    seed_cyclists_max: int = 8
    seed_radius_m: float = 120.0
    #: ⚠ GTA ignites a vehicle only when its ENGINE health goes negative, which a
    #: road collision does not do -- measured tank_health_min 984.9/1000 with
    #: on_fire false on every clip. Ignite explicitly once a vehicle is wrecked.
    ignite_wrecks: bool = True

    # --- actor behaviour ----------------------------------------------------
    #: ★ How every non-ego road user drives. These used to be private members in
    #: the plugin with no message, so the build could only produce one flavour of
    #: traffic. They are per-clip now because counterfactual capture replays the
    #: SAME scene with the actors sane in one clip and reckless in the next.
    #: Defaults are the max-chaos values the plugin shipped with.
    npc_driving_style: int = 512 | 262144   # ALLOW_WRONG_WAY | TAKE_SHORTEST_PATH
    npc_aggressiveness: float = 1.0
    npc_ability: float = 0.0                # LOWER is worse driving
    npc_cruise_speed: float = 40.0          # m/s
    npc_steers_around: bool = False         # True = stock avoidance
    traffic_aggression: bool = True         # master switch for the re-task sweep
    ped_interaction: bool = True            # master switch for peds crossing
    ped_cross_chance: float = 0.35

    #: ★ Reproducible scenes. When true the game's ambient population is cleared
    #: and its spawner held at zero for the clip, and every seeded actor's model
    #: is chosen from the scene seed -- so the same scene yields the same people
    #: and cars in every variation and on every rerun. The cost is no background
    #: traffic beyond what is seeded; raise seed_* counts to compensate. Physics
    #: and AI are still not bit-exact across runs: same situation, not same pixels.
    deterministic_population: bool = False
    #: A variation whose recorded start is further than this from the scene's
    #: requested point is not that scene and is rejected. Observed: two kept
    #: variations of one scene 986 m apart, because warmup released on proximity
    #: before the relocate had happened.
    scene_max_offset_m: float = 150.0
    #: Stationary window allowed before "stuck" for a LAWFUL ego (restraint bits
    #: >= 0.5). Lawful driving stops behind traffic and at junctions; the 4 s
    #: default was tuned for a style that never does.
    stuck_seconds_lawful: float = 14.0
    #: ★ Chosen from measurement, not guessed. The first value (400) produced zero
    #: fires across 26 clips -- not because ignition was broken but because nothing
    #: ever got that low. ⚠ GTA road collisions at these speeds do not destroy
    #: cars: the worst-hit vehicle across a seeded set reached body health 766,
    #: median 809, from a starting 1000. 850 fires on the worst-hit vehicle in
    #: roughly 3 of 4 clips; drop it toward 800 for a rarer, harder-impact-only
    #: fire, or raise it toward 900 to ignite on scrapes.
    ignite_below_health: float = 850.0

    #: Optional {region_name: weight} override of coverage.REGIONS' built-in
    #: weights. Weight 0 drops the region entirely. Use for validation runs that
    #: must target a specific part of the map, or a targeted top-up; leave empty
    #: for the default urban-weighted schedule.
    region_weights: dict = field(default_factory=dict)
    #: ⚠ Arrival gets a longer deadline than warmup: a cross-map relocate can take
    #: ~20 s to land, and cutting it short does not save time -- it produces a clip
    #: recorded at the previous location, thrown away whole.
    arrive_timeout_s: float = 30.0
    #: Hard backstop: any in-clip position discontinuity larger than this (and
    #: larger than 60 m/s x the frame gap) aborts the clip, whatever caused it.
    max_jump_m: float = 50.0

    # --- event kinematics -----------------------------------------------
    #: ★ Sample the time-to-collision you WANT, then solve backwards for spawn
    #: geometry. Sampling geometry and letting TTC fall out gives a broad,
    #: mostly-boring distribution; this makes difficulty a controlled axis.
    ttc_min: float = 0.6
    ttc_max: float = 3.0

    # --- ambient density -------------------------------------------------
    #: ⚠ Upstream had no density control at all -- zero of these natives appear
    #: anywhere in the codebase. They are *_THIS_FRAME natives, re-applied every
    #: tick by the plugin.
    density_vehicle_min: float = 0.6
    density_vehicle_max: float = 3.0
    density_ped_min: float = 0.3
    density_ped_max: float = 3.0
    density_parked_min: float = 0.2
    density_parked_max: float = 2.0

    # --- time scale ------------------------------------------------------
    #: ★ At 30 m/s an impact traverses ~1.5 m per 50 ms, so the most informative
    #: half-second of a clip otherwise gets the fewest frames. Slowing time
    #: around the trigger spends more frames there for the same in-game duration.
    #: ⚠ Costs proportionally more wall clock for that window.
    #: ⚠ OFF by default. The idea is sound -- at 30 m/s an impact traverses ~1.5 m
    #: per 50 ms, so the most informative half-second otherwise gets the fewest
    #: frames, and time scale 0.22 spends ~4.5x the frames there. Rolling a COIN
    #: for it is not: it makes the dataset heterogeneous in a way that is a
    #: property of the capture rig rather than of driving, and it silently breaks
    #: anything assuming uniform fps. It did exactly that to the video encoder --
    #: one constant fps across a clip whose rate varied 10:1 played the normal
    #: section 1.4x fast AND the slow-mo section 3.4x slow.
    #:
    #: ⚠ dt is NOT uniform even at 0.0: the plugin pauses per frame, so real
    #: sampling still ranges ~14-35 Hz. Anything quantitative must use the
    #: game_time_ms in poses.jsonl, never 1/rate_hz.
    #:
    #: If dense impact sampling is wanted, capture it as a deliberate labelled
    #: subset with this at 1.0, not as a coin flip inside the main set.
    slowmo_probability: float = 0.0
    slowmo_scale: float = 0.25
    slowmo_lead_s: float = 0.4       # start before the trigger
    slowmo_hold_s: float = 1.6       # in-game seconds at reduced scale

    # --- stuck / overturned ----------------------------------------------
    #: Below this speed (m/s) the ego is not meaningfully driving.
    stuck_speed_mps: float = 1.0
    #: Don't judge before this point -- a clip legitimately starts slow.
    stuck_grace_s: float = 3.0
    #: Abort once it has been immobile this long. A stationary clip passes every
    #: quality gate (valid frames, valid poses) while containing nothing.
    stuck_seconds: float = 4.0

    # --- outcome bias ----------------------------------------------------
    #: Chase under-represented OUTCOMES rather than under-represented setups.
    outcome_bias: bool = True
    #: >1 chases rare outcomes harder. Measured on a synthetic 3-scenario world:
    #: off -> 57.1% no_event / 1.538 bits; T=1 -> 49.2% / 1.663; T=2 -> 45.4% / 1.703.
    #: The floor is the best scenario's own no_event rate, so this cannot reach 0.
    outcome_bias_temperature: float = 1.5
    #: Clips a scenario needs before its measured yield replaces its static weight.
    outcome_bias_min_history: int = 8

    # --- camera mount (relative to the ego vehicle) ---------------------
    # ⚠ These are now DELTAS on a mount derived from the ego's bounding box
    # (front edge, roof height). Upstream's fixed 0.5/0.8 from the vehicle ORIGIN
    # put the camera inside the cabin -- wheel, hands and dash filling the frame.
    #: ⚠ Camera mount, as a fraction of the ego's half-extent. Was effectively
    #: 0.90 forward -- on the front bumper -- so a head-on pedestrian impact
    #: happened UNDER the camera and out of frame. 0.35 is a windscreen position.
    #: Changing this changes the extrinsics; clips either side are not
    #: geometrically comparable, though each clip's own pose is exported per frame.
    cam_mount_forward_frac: float = 0.35
    cam_mount_up_frac: float = 0.62

    cam_forward: float = 0.0
    cam_right: float = 0.0
    cam_up: float = 0.0
    cam_rot_x: float = 0.0
    cam_rot_y: float = 0.0
    cam_rot_z: float = 0.0

    # --- environment variation ------------------------------------------
    weathers: list = field(default_factory=lambda: [
        "CLEAR", "EXTRASUNNY", "CLOUDS", "OVERCAST", "RAIN", "CLEARING",
        "THUNDER", "SMOG", "FOGGY", "SNOWLIGHT", "BLIZZARD", "NEUTRAL", "SNOW",
    ])
    hours: list = field(default_factory=lambda: list(range(24)))

    # --- quality gates ---------------------------------------------------
    #: Reject a clip if a frame-to-frame photometric jump is not explained by
    #: camera motion -- the signature of LOD pop-in / streaming.
    popin_reject: bool = True
    popin_photometric_threshold: float = 28.0   # mean abs 0-255 delta
    popin_motion_threshold: float = 0.9         # metres of camera translation
    #: Isolated pop-in frames are tolerable; a structurally broken clip shows many.
    #: Measured on real runs: good clips carry 0-3 events across ~350 frames, so
    #: rejecting on >0 discarded roughly half of every batch.
    popin_max_events: int = 6
    #: Reject clips where the ego never moved (spawn failure, stuck on geometry).
    min_travelled_metres: float = 8.0

    # --- counterfactual variations -----------------------------------------
    #: The variation plan the capture runner hands over inside the same JSON:
    #: a list of [Variation, generator_config_dict] pairs, where Variation is
    #: {"name", "ego_chaos", "actor_chaos"}. Empty means single clips, as before.
    #: run_generator reads it; nothing in this class interprets it.
    #:
    #: A real dataclass field rather than a key load() strips off, so that every
    #: way of building a config -- CaptureConfig(**d), load(), clone() -- accepts
    #: the key and carries it, and asdict()/save() write it back out. That puts the
    #: plan into the capture_config.json the generator records, which is the one
    #: place a run's provenance is kept.
    #: ⚠ Per-variation configs built from the pairs must NOT inherit this list:
    #: run_generator pins it back to [] on each, or a variation config would look
    #: like it planned variations of its own.
    variation_plan: list = field(default_factory=list)

    def clone(self, **overrides):
        d = asdict(self)
        d.update(overrides)
        return CaptureConfig(**d)

    @staticmethod
    def load(path):
        if not path:
            return CaptureConfig()
        with open(path) as f:
            return CaptureConfig(**json.load(f))

    def save(self, path):
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)
