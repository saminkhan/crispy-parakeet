"""Long-tail event library.

Each scenario stages one catastrophic or near-catastrophic event from the ego's
point of view. The contract:

    setup(ctx)    - during WARMUP (not recorded): spawn actors into position
    trigger(ctx)  - at t = trigger_offset inside the clip: make it happen
    tick(ctx, t)  - optional, every frame of the clip

Design notes worth keeping:

★ Reactivity is free. GTA V's ambient traffic genuinely brakes, swerves and
  honks when the ego misbehaves -- but ONLY if SET_EVERYONE_IGNORE_PLAYER is
  off. That is `SetSurvivalMode(everyoneIgnore=False)`, which the episode runner
  sends. Half the scenarios below need no spawned actors at all: they just make
  the ego misbehave and let the world react.

⚠ Reactivity is game-tuned, not physically calibrated. Reaction times and
  braking profiles are arcade-y. Fine as world-model training signal for "what
  does a plausible near-miss look like"; not a validated behaviour model.
"""

import random

from deepgtav.messages import (
    CreatePed, CreateVehicle, SetEgoDrivingMode, SetWeather,
    TaskVehicleDriveToCoord, TaskVehicleTempAction,
)
import drivingstyles as ds

EGO = -1


# ---------------------------------------------------------------------------
# ★ Time-to-collision parameterisation.
#
# Sampling spawn geometry and letting TTC fall out produces a broad, mostly
# boring distribution. Sampling the TTC you want and solving backwards for the
# geometry turns event difficulty into a controlled axis -- and TTC is the single
# most informative parameter of a conflict.
# ---------------------------------------------------------------------------

def ttc_head_on(ttc, v_ego, v_actor):
    """Actor approaching from ahead. Closing speed is the sum."""
    return max(6.0, ttc * (v_ego + v_actor))


def ttc_lead(ttc, v_ego, v_actor):
    """Actor ahead travelling the same way. Closing speed is the difference.

    Clamped: if the actor is not slower than the ego there is no conflict, so we
    fall back to a short spacing and let the trigger (a brake) create the closing
    speed instead.
    """
    closing = v_ego - v_actor
    if closing < 1.0:
        return max(6.0, ttc * max(3.0, v_ego * 0.35))
    return max(6.0, ttc * closing)


def ttc_cross(ttc, v_ego, v_actor):
    """Both reach the same point at t = ttc. Returns (forward, lateral)."""
    return max(6.0, ttc * v_ego), max(6.0, ttc * v_actor)



def _scene_ped_model(ctx):
    """A pedestrian model drawn from the SCENE stream, not the global module.

    ⚠ CreatePed(model=None) falls back to utils.PedNamesAndHashes.getRandomPed(),
    which is random.sample() on the process-wide generator -- so the two scenarios
    that spawn peds got a different person in every variation of the same scene,
    and a different one on every rerun. That was the one leak the determinism
    review found; everything else in a scene already comes from ctx.rng. Same
    pool, scene rng.
    """
    from utils.PedNamesAndHashes import spawnablePeds_ls
    return ctx.rng.choice(spawnablePeds_ls)

class Scenario:
    name = "base"
    weight = 1.0
    #: Scenarios that need the ego to be moving fast to be interesting.
    min_ego_speed = 0.0

    def setup(self, ctx):
        return {}

    def trigger(self, ctx):
        return {}

    def tick(self, ctx, t):
        return None


# ---------------------------------------------------------------------------
# Ego-caused events. No spawned actors: the ego misbehaves, the world reacts.
# ---------------------------------------------------------------------------

class EgoRunsRedLight(Scenario):
    name = "ego_runs_red_light"
    weight = 3.0
    min_ego_speed = 6.0

    def trigger(self, ctx):
        speed = ctx.rng.uniform(22.0, 34.0)
        ctx.send(SetEgoDrivingMode(drivingMode=ds.IGNORE_LIGHTS, setSpeed=speed))
        return {"style": "IGNORE_LIGHTS", "target_speed": speed}


class EgoIntoCrossTraffic(Scenario):
    """The diagram's canonical case: enter an intersection against traffic and
    let the cross-traffic AI react."""
    name = "ego_into_cross_traffic"
    weight = 3.0
    min_ego_speed = 6.0

    def trigger(self, ctx):
        speed = ctx.rng.uniform(24.0, 36.0)
        ctx.send(SetEgoDrivingMode(drivingMode=ds.PLOUGH_THROUGH, setSpeed=speed))
        return {"style": "PLOUGH_THROUGH", "target_speed": speed}


class EgoSwerveIntoOncoming(Scenario):
    name = "ego_swerve_into_oncoming"
    weight = 3.0
    min_ego_speed = 8.0

    def trigger(self, ctx):
        # Left-hand swerve crosses the centre line on right-hand-drive roads.
        action = TaskVehicleTempAction.SWERVE_LEFT
        dur = int(ctx.rng.uniform(1200, 2600))
        ctx.send(TaskVehicleTempAction(actorId=EGO, action=action, durationMs=dur))
        return {"action": "SWERVE_LEFT", "duration_ms": dur}


class EgoWrongWay(Scenario):
    name = "ego_wrong_way"
    weight = 2.0
    min_ego_speed = 6.0

    def trigger(self, ctx):
        mode = ds.AGGRESSIVE_PRESETS["wrong_way"]
        speed = ctx.rng.uniform(18.0, 30.0)
        ctx.send(SetEgoDrivingMode(drivingMode=mode, setSpeed=speed))
        return {"style": "wrong_way", "target_speed": speed}


class EgoLossOfControl(Scenario):
    name = "ego_loss_of_control"
    weight = 2.0
    min_ego_speed = 10.0

    def setup(self, ctx):
        # Low grip is the cheapest way to get a plausible skid.
        w = ctx.rng.choice(["RAIN", "THUNDER", "BLIZZARD", "SNOW"])
        ctx.send(SetWeather(w))
        return {"weather_override": w}

    def trigger(self, ctx):
        action = ctx.rng.choice([TaskVehicleTempAction.HARD_TURN_LEFT,
                                 TaskVehicleTempAction.HARD_TURN_RIGHT])
        dur = int(ctx.rng.uniform(900, 2000))
        ctx.send(TaskVehicleTempAction(actorId=EGO, action=action, durationMs=dur))
        return {"action": int(action), "duration_ms": dur}


class EgoEmergencyBrake(Scenario):
    name = "ego_emergency_brake"
    weight = 1.5
    min_ego_speed = 8.0

    def trigger(self, ctx):
        dur = int(ctx.rng.uniform(1500, 3000))
        ctx.send(TaskVehicleTempAction(actorId=EGO, action=TaskVehicleTempAction.BRAKE,
                                       durationMs=dur))
        return {"action": "BRAKE", "duration_ms": dur}


# ---------------------------------------------------------------------------
# Actor-caused events. Spawn during warmup, trigger inside the clip.
# ---------------------------------------------------------------------------

class LeadVehicleBrakeCheck(Scenario):
    name = "lead_vehicle_brake_check"
    weight = 3.0
    min_ego_speed = 8.0

    def setup(self, ctx):
        self.actor = ctx.new_actor_id()
        v_ego = ctx.ego_speed()
        v_act = v_ego * ctx.rng.uniform(0.75, 1.0)
        ttc = ctx.sample_ttc()
        fwd = ttc_lead(ttc, v_ego, v_act)
        ctx.send(CreateVehicle(model=ctx.rng.choice(ctx.traffic_models),
                               relativeForward=fwd, relativeRight=0.0, heading=0.0,
                               color=-1, color2=-1, placeOnGround=True,
                               speed=v_act, drivingMode=ds.NORMAL,
                               actorId=self.actor))
        return {"actor_id": self.actor, "spawn_forward": round(fwd, 2),
                "ttc_s": round(ttc, 3), "v_ego": round(v_ego, 2), "v_actor": round(v_act, 2)}

    def trigger(self, ctx):
        dur = int(ctx.rng.uniform(1800, 3500))
        ctx.send(TaskVehicleTempAction(actorId=self.actor,
                                       action=TaskVehicleTempAction.BRAKE, durationMs=dur))
        return {"action": "BRAKE", "duration_ms": dur}


class CutIn(Scenario):
    name = "cut_in"
    weight = 3.0
    min_ego_speed = 8.0

    def setup(self, ctx):
        self.actor = ctx.new_actor_id()
        self.side = ctx.rng.choice([-1.0, 1.0])
        v_ego = ctx.ego_speed()
        v_act = v_ego * ctx.rng.uniform(0.9, 1.25)
        ttc = ctx.sample_ttc()
        fwd = ttc_lead(ttc, v_ego, v_act)
        ctx.send(CreateVehicle(model=ctx.rng.choice(ctx.traffic_models),
                               relativeForward=fwd, relativeRight=self.side * 3.6,
                               heading=0.0, color=-1, color2=-1, placeOnGround=True,
                               speed=v_act, drivingMode=ds.RUSHED,
                               actorId=self.actor))
        return {"actor_id": self.actor, "side": self.side, "spawn_forward": round(fwd, 2),
                "ttc_s": round(ttc, 3), "v_ego": round(v_ego, 2), "v_actor": round(v_act, 2)}

    def trigger(self, ctx):
        action = (TaskVehicleTempAction.SWERVE_RIGHT if self.side < 0
                  else TaskVehicleTempAction.SWERVE_LEFT)
        dur = int(ctx.rng.uniform(900, 1800))
        ctx.send(TaskVehicleTempAction(actorId=self.actor, action=action, durationMs=dur))
        return {"action": int(action), "duration_ms": dur}


class OncomingHeadOn(Scenario):
    name = "oncoming_head_on"
    weight = 2.5
    min_ego_speed = 6.0

    def setup(self, ctx):
        self.actor = ctx.new_actor_id()
        v_ego = ctx.ego_speed()
        self.v_act = ctx.rng.uniform(18.0, 32.0)
        self.ttc = ctx.sample_ttc()
        fwd = ttc_head_on(self.ttc, v_ego, self.v_act)
        ctx.send(CreateVehicle(model=ctx.rng.choice(ctx.traffic_models),
                               relativeForward=fwd, relativeRight=ctx.rng.uniform(-4.0, 4.0),
                               heading=180.0, color=-1, color2=-1, placeOnGround=True,
                               speed=2.0, drivingMode=ds.NORMAL, actorId=self.actor))
        return {"actor_id": self.actor, "spawn_forward": round(fwd, 2),
                "ttc_s": round(self.ttc, 3), "v_ego": round(v_ego, 2),
                "v_actor": round(self.v_act, 2)}

    def trigger(self, ctx):
        # Drive it straight at wherever the ego is right now.
        ex, ey, ez = ctx.ego_position()
        ctx.send(TaskVehicleDriveToCoord(actorId=self.actor, x=ex, y=ey, z=ez,
                                         speed=self.v_act, drivingMode=ds.STRAIGHT_LINE))
        return {"target": [ex, ey, ez], "speed": round(self.v_act, 2)}


class SideImpactRunner(Scenario):
    """A vehicle crossing the ego's path from the side, ignoring pathing."""
    name = "side_impact_runner"
    weight = 2.5
    min_ego_speed = 6.0

    def setup(self, ctx):
        self.actor = ctx.new_actor_id()
        self.side = ctx.rng.choice([-1.0, 1.0])
        v_ego = ctx.ego_speed()
        self.v_act = ctx.rng.uniform(14.0, 28.0)
        self.ttc = ctx.sample_ttc()
        fwd, lat = ttc_cross(self.ttc, v_ego, self.v_act)
        ctx.send(CreateVehicle(model=ctx.rng.choice(ctx.traffic_models),
                               relativeForward=fwd, relativeRight=self.side * lat,
                               heading=-90.0 * self.side, color=-1, color2=-1,
                               placeOnGround=True, speed=2.0,
                               drivingMode=ds.NORMAL, actorId=self.actor))
        return {"actor_id": self.actor, "side": self.side, "ttc_s": round(self.ttc, 3),
                "spawn_forward": round(fwd, 2), "spawn_lateral": round(lat, 2),
                "v_ego": round(v_ego, 2), "v_actor": round(self.v_act, 2)}

    def trigger(self, ctx):
        ex, ey, ez = ctx.ego_position()
        ctx.send(TaskVehicleDriveToCoord(actorId=self.actor, x=ex, y=ey, z=ez,
                                         speed=self.v_act, drivingMode=ds.STRAIGHT_LINE))
        return {"target": [ex, ey, ez], "speed": round(self.v_act, 2)}


class StalledObstacle(Scenario):
    """Stationary hazard in-lane -- the classic 'why did the AV not stop' case."""
    name = "stalled_obstacle"
    weight = 2.0
    min_ego_speed = 8.0

    def setup(self, ctx):
        self.actors = []
        n = ctx.rng.randint(1, 3)
        base = ctx.rng.uniform(30.0, 60.0)
        for i in range(n):
            aid = ctx.new_actor_id()
            self.actors.append(aid)
            ctx.send(CreateVehicle(model=ctx.rng.choice(ctx.traffic_models),
                                   relativeForward=base + i * ctx.rng.uniform(4.0, 9.0),
                                   relativeRight=ctx.rng.uniform(-2.5, 2.5),
                                   heading=ctx.rng.uniform(-60.0, 60.0),
                                   color=-1, color2=-1, placeOnGround=True,
                                   speed=0.0, drivingMode=ds.NORMAL, actorId=aid))
        return {"actor_ids": self.actors, "count": n, "base_forward": base}

    def trigger(self, ctx):
        speed = ctx.rng.uniform(20.0, 32.0)
        ctx.send(SetEgoDrivingMode(drivingMode=ds.PLOUGH_THROUGH, setSpeed=speed))
        return {"ego_style": "PLOUGH_THROUGH", "target_speed": speed}


class EmergencyScene(Scenario):
    """Static roadside incident: emergency vehicles plus bystanders.

    ⚠ Bystanders here are spawned, not choreographed: CreatePed's `task` field
    exists in the Python message but the C++ handler drops it, and there is no
    ped-tasking command. A true dart-out needs one more native
    (TASK_GO_STRAIGHT_TO_COORD on a spawned ped) wired through Server.cpp.
    """
    name = "emergency_scene"
    weight = 1.5

    def setup(self, ctx):
        self.actors = []
        fwd = ctx.rng.uniform(30.0, 55.0)
        for model in ctx.rng.sample(["firetruk", "ambulance", "police", "police2"], 2):
            aid = ctx.new_actor_id()
            self.actors.append(aid)
            ctx.send(CreateVehicle(model=model, relativeForward=fwd + ctx.rng.uniform(-6, 6),
                                   relativeRight=ctx.rng.uniform(-4.5, 4.5),
                                   heading=ctx.rng.uniform(0, 360), color=-1, color2=-1,
                                   placeOnGround=True, speed=0.0,
                                   drivingMode=ds.NORMAL, actorId=aid))
        for _ in range(ctx.rng.randint(2, 5)):
            ctx.send(CreatePed(relativeForward=fwd + ctx.rng.uniform(-8, 8),
                               model=_scene_ped_model(ctx),
                               relativeRight=ctx.rng.uniform(-6, 6), relativeUp=0.0,
                               heading=ctx.rng.uniform(0, 360), placeOnGround=True))
        return {"actor_ids": self.actors, "scene_forward": fwd}

    def trigger(self, ctx):
        speed = ctx.rng.uniform(16.0, 26.0)
        ctx.send(SetEgoDrivingMode(drivingMode=ds.RUSHED, setSpeed=speed))
        return {"ego_style": "RUSHED", "target_speed": speed}


class PedestrianHazard(Scenario):
    """Peds placed in the carriageway ahead. See EmergencyScene's caveat."""
    name = "pedestrian_hazard"
    weight = 1.5
    min_ego_speed = 6.0

    def setup(self, ctx):
        fwd = ctx.rng.uniform(14.0, 30.0)
        n = ctx.rng.randint(2, 5)
        for _ in range(n):
            ctx.send(CreatePed(relativeForward=fwd + ctx.rng.uniform(-3, 3),
                               model=_scene_ped_model(ctx),
                               relativeRight=ctx.rng.uniform(-4.0, 4.0), relativeUp=0.0,
                               heading=ctx.rng.uniform(0, 360), placeOnGround=True))
        return {"count": n, "spawn_forward": fwd}

    def trigger(self, ctx):
        speed = ctx.rng.uniform(16.0, 28.0)
        ctx.send(SetEgoDrivingMode(drivingMode=ds.PLOUGH_THROUGH, setSpeed=speed))
        return {"ego_style": "PLOUGH_THROUGH", "target_speed": speed}


ALL_SCENARIOS = [
    EgoRunsRedLight, EgoIntoCrossTraffic, EgoSwerveIntoOncoming, EgoWrongWay,
    EgoLossOfControl, EgoEmergencyBrake, LeadVehicleBrakeCheck, CutIn,
    OncomingHeadOn, SideImpactRunner, StalledObstacle, EmergencyScene,
    PedestrianHazard,
]

TRAFFIC_MODELS = [
    "blista", "sultan", "asea", "futo", "baller", "bison", "burrito", "dubsta",
    "granger", "gresley", "ingot", "intruder", "landstalker", "minivan",
    "premier", "primo", "radi", "rebel", "sadler", "seminole", "stanier",
    "stratum", "tailgater", "washington", "phoenix", "ruiner", "packer",
    "pounder", "mule", "benson", "biff",
]


def pick(rng, ego_speed=None, bias=None):
    """Weighted choice, filtered by whether the ego is fast enough to matter.

    `bias` is an outcomes.OutcomeBiased instance. When supplied, weights come
    from observed P(outcome | scenario) rather than the static class weights, so
    selection chases under-represented OUTCOMES instead of under-represented
    setups. That distinction is the whole point -- see longtail/outcomes.py.
    """
    pool = [s for s in ALL_SCENARIOS
            if ego_speed is None or ego_speed >= s.min_ego_speed]
    if not pool:
        pool = list(ALL_SCENARIOS)
    weights = bias.weights(pool) if bias is not None else [s.weight for s in pool]
    return rng.choices(pool, weights=weights, k=1)[0]()
