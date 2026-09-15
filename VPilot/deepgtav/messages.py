#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import numpy as np
from numpy.lib.stride_tricks import as_strided

import utils.PedNamesAndHashes as PedNamesAndHashes
from utils.Constants import FRAME, SCREEN_RESOLUTION

class Scenario:
    def __init__(self, location=None, time=None, weather=None, vehicle=None, drivingMode=None, spawnedEntitiesDespawnSeconds=None):
        self.location = location #[x,y,z,heading] (heading optional)
        self.time = time #[hour, minute]
        self.weather = weather #string
        self.vehicle = vehicle #string
        self.drivingMode = drivingMode #[drivingMode, setSpeed]
        self.spawnedEntitiesDespawnSeconds = spawnedEntitiesDespawnSeconds # Despawn time in seconds


# TODO Default settings should be added in the future
class Dataset:
    def __init__(self, **kwargs):
            # rate=None, frame=None, screenResolution=None, vehicles=None, peds=None, trafficSigns=None, direction=None, reward=None, 
            # throttle=None, brake=None, steering=None, speed=None, yawRate=None, drivingMode=None, location=None, time=None,
            # offscreen=None, showBoxes=None, pointclouds=None, stationaryScene=None, vehiclesToCreate=None, pedsToCreate=None,
            # startIndex=None, lidarParam=None, collectTracking=None, recordScenario=None, positionScenario=None):
        self.__dict__.update(kwargs)
        
        if not 'frame' in kwargs:
            self.frame = FRAME
        if not 'screenResolution' in kwargs:
            self.screenResolution = SCREEN_RESOLUTION

        # self.rate = rate #Hz
        # self.frame = frame #[width, height]
        # self.screenResolution = screenResolution # [width, height]


        # self.vehicles = vehicles #boolean
        # self.peds = peds #boolean
        # self.trafficSigns = trafficSigns #boolean
        # self.direction = direction #[x,y,z]
        # self.reward = reward #[id, p1, p2]
        # self.throttle = throttle #boolean
        # self.brake = brake #boolean
        # self.steering = steering #boolean
        # self.speed = speed #boolean
        # self.yawRate = yawRate #boolean
        # self.drivingMode = drivingMode #boolean
        # self.location = location #boolean
        # self.time = time #boolean


        # self.offscreen = offscreen #boolean
        # self.showBoxes = showBoxes #boolean
        # self.pointclouds = pointclouds #boolean
        # self.stationaryScene = stationaryScene #boolean
        # self.vehiclesToCreate = vehiclesToCreate #array of [model, pos.forward, pos.right, heading, color]
        # self.pedsToCreate = pedsToCreate #array of peds
        # self.startIndex = startIndex #int
        # self.lidarParam = lidarParam #int
        # self.collectTracking = collectTracking #boolean
        # self.recordScenario = recordScenario #boolean (for recording clips)
        # self.positionScenario = positionScenario #boolean (for getting current location)

class Start:
    def __init__(self, scenario=None, dataset=None):
        self.scenario = scenario
        self.dataset = dataset

    def to_json(self):
        _scenario = None
        _dataset = None

        if (self.scenario != None):
            _scenario = self.scenario.__dict__

        if (self.dataset != None):
            _dataset = self.dataset.__dict__            

        return json.dumps({'start':{'scenario': _scenario, 'dataset': _dataset}})


class Config:
    def __init__(self, scenario=None, dataset=None):
        self.scenario = scenario
        self.dataset = dataset

    def to_json(self):
        _scenario = None
        _dataset = None

        if (self.scenario != None):
            _scenario = self.scenario.__dict__

        if (self.dataset != None):
            _dataset = self.dataset.__dict__            

        return json.dumps({'config':{'scenario': _scenario, 'dataset': _dataset}})

class Stop:
    def to_json(self):
        return json.dumps({'stop':None}) #super dummy

class Commands:
    def __init__(self, throttle=None, brake=None, steering=None):
        self.throttle = throttle #float (0,1)
        self.brake = brake #float (0,1)
        self.steering = steering #float (-1,1)

    def to_json(self):
        return json.dumps({'commands':self.__dict__})
        
def frame2numpy(frame, frameSize=SCREEN_RESOLUTION):
    buff = np.frombuffer(frame, dtype='uint8')
    # Scanlines are aligned to 4 bytes in Windows bitmaps
    strideWidth = int((frameSize[0] * 3 + 3) / 4) * 4
    # Return a copy because custom strides are not supported by OpenCV.
    return as_strided(buff, strides=(strideWidth, 3, 1), shape=(frameSize[1], frameSize[0], 3)).copy()

# TODO not yet implemented in DeepGTAV-PreSIL
class StartRecording:
    def to_json(self):
        return json.dumps({'StartRecording':None}) #super dummy

# TODO not yet implemented in DeepGTAV-PreSIL
class StopRecording:
    def to_json(self):
        return json.dumps({'StopRecording':None}) #super dummy



# A command to use the AI in GTAV to walk to the specified location, from the current location
class GoToLocation:
    def __init__(self, x, y, z, speed = 20.0):
        self.x = x
        self.y = y
        self.z = z
        self.speed = speed
    
    def to_json(self):
        return json.dumps({'GoToLocation':self.__dict__})
    

# A command to Teleport to the specified location
class TeleportToLocation:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z

    def to_json(self):
        return json.dumps({'TeleportToLocation':self.__dict__})


# A command to set the camera position and rotation (relative to the vehicle position and vehicle axis).
# Angles are in Degree 
# Default is to directly look forward

# in this context the x-axis is right, the y-axis is forward and the z-axis is up.
# Rotations are applied in the order rot_z, rot_y, rot_x
class SetCameraPositionAndRotation:
    def __init__(self, x=0, y=0, z=0, rot_x=0, rot_y=0, rot_z=0):
        self.x = x
        self.y = y 
        self.z = z
        self.rot_x = rot_x
        self.rot_y = rot_y
        self.rot_z = rot_z
    
    def to_json(self):
        return json.dumps({'SetCameraPositionAndRotation':self.__dict__})


# Creates a Pedestrian relative to the player vehicle. The model can be set with
# a model name, which can be found e.g. at:
# https://docs.fivem.net/docs/game-references/ped-models/ Note that there are
# different versions of most models, with different textures.

# Normally models wander in the game world. A specific animation can be given
# with an animDict and the animName. Those can be found at
# https://alexguirre.github.io/animations-list/
class CreatePed:
    def __init__(self, relativeForward = 0, relativeRight = 0, relativeUp = 0, model = None, heading = 0, task = 0, placeOnGround = True, animDict = "", animName = ""):
        
        if model == None:
            model = PedNamesAndHashes.getRandomPed()

        if isinstance(model, str):
            model = PedNamesAndHashes.convertModelNameToHash(model) 
        
        self.model = model
        self.relativeForward = relativeForward
        self.relativeRight = relativeRight
        self.relativeUp = relativeUp
        self.heading = heading
        self.task = task
        self.placeOnGround = placeOnGround
        self.animDict = animDict
        self.animName = animName
    
    def to_json(self):
        return json.dumps({'CreatePed':self.__dict__})


class CreateVehicle:
    # [longtail] speed / drivingMode / actorId are additions on the longtail branch.
    # Upstream hard-coded TASK_VEHICLE_DRIVE_WANDER(ped, veh, 2.0f, 16777216) inside
    # Scenario::createVehicle, so every spawned vehicle crawled at 2 m/s -- useless as
    # oncoming traffic or a cut-in. actorId is a client-assigned handle so the vehicle
    # can be tasked later with TaskVehicleTempAction / TaskVehicleDriveToCoord.
    # The C++ treats all three as optional, so older scripts are unaffected.
    def __init__(self, model="Blista", relativeForward=0, relativeRight=0, heading=0, color=0, color2=0, placeOnGround=True, withLifeJacketPed=False,
                 speed=2.0, drivingMode=16777216, actorId=-1):
        self.model = model
        self.relativeForward = relativeForward
        self.relativeRight = relativeRight
        self.heading = heading
        self.color = color
        self.color2 = color2
        self.placeOnGround = placeOnGround
        self.withLifeJacketPed = withLifeJacketPed
        self.speed = speed
        self.drivingMode = drivingMode
        self.actorId = actorId
    def to_json(self):
        return json.dumps({'CreateVehicle':self.__dict__})


# weather can be one of { "CLEAR", "EXTRASUNNY", "CLOUDS", "OVERCAST", "RAIN", "CLEARING", "THUNDER", "SMOG", "FOGGY", "XMAS", "SNOWLIGHT", "BLIZZARD", "NEUTRAL", "SNOW" }
class SetWeather:
    def __init__(self, weather):
        self.weather = weather
    
    def to_json(self):
        return json.dumps({'SetWeather':self.__dict__})

class SetClockTime:
    def __init__(self, hour = 0, minute = 0, second = 0):
        self.hour = hour 
        self.minute = minute
        self.second = second
    
    def to_json(self):
        return json.dumps({'SetClockTime':self.__dict__})


# ============================================================================
# [longtail] Long-tail event orchestration
# ----------------------------------------------------------------------------
# These require the DeepGTAV-PreSIL patches on the `longtail` branch. Sending
# them to a stock plugin build is a no-op (the dispatcher falls through to
# "Invalid message" and returns).
# ============================================================================

class SetSurvivalMode:
    """Choose which of the upstream 'keep the run alive' guards stay on.

    Upstream applies all of them unconditionally every 10 s, which is why stock
    DeepGTAV cannot capture collisions, damage, fire, or reactive traffic.

    The capture preset is: player survives, car does not, everyone reacts.
      - everyoneIgnore=False   -> NPCs react to the ego (brake, swerve, honk)
      - vehicleInvincible=False-> the car deforms, burns, and can be wrecked
      - playerInvincible=True  -> no death/hospital cutscene stealing the camera
      - seatbelt=True          -> ped is not ejected, so the ego loop stays intact
    """

    def __init__(self, policeIgnore=True, everyoneIgnore=False, playerInvincible=True,
                 vehicleInvincible=False, seatbelt=True, aggressiveness=0.0, ability=100.0):
        self.policeIgnore = policeIgnore
        self.everyoneIgnore = everyoneIgnore
        self.playerInvincible = playerInvincible
        self.vehicleInvincible = vehicleInvincible
        self.seatbelt = seatbelt
        self.aggressiveness = aggressiveness
        self.ability = ability

    def to_json(self):
        return json.dumps({'SetSurvivalMode': self.__dict__})


class TaskVehicleTempAction:
    """Scripted swerve / brake. actorId -1 = ego, >= 0 = a CreateVehicle actor.

    Action ids are the game's own. Verify against NativeDB for your build before
    trusting them; the ones below are the commonly cited values.
    """
    BRAKE = 1
    HARD_TURN_LEFT = 6
    HARD_TURN_RIGHT = 7
    SWERVE_LEFT = 9
    SWERVE_RIGHT = 10

    def __init__(self, actorId=-1, action=BRAKE, durationMs=2000):
        self.actorId = actorId
        self.action = action
        self.durationMs = durationMs

    def to_json(self):
        return json.dumps({'TaskVehicleTempAction': self.__dict__})


class TaskVehicleDriveToCoord:
    def __init__(self, actorId=-1, x=0.0, y=0.0, z=0.0, speed=20.0, drivingMode=786603):
        self.actorId = actorId
        self.x = x
        self.y = y
        self.z = z
        self.speed = speed
        self.drivingMode = drivingMode

    def to_json(self):
        return json.dumps({'TaskVehicleDriveToCoord': self.__dict__})


class SetEgoDrivingMode:
    """Re-task the ego mid-clip. The driving-style bitmask is how the ego runs a
    red light or crosses the centre line -- see longtail.drivingstyles."""

    def __init__(self, drivingMode=786603, setSpeed=15.0):
        self.drivingMode = drivingMode
        self.setSpeed = setSpeed

    def to_json(self):
        return json.dumps({'SetEgoDrivingMode': self.__dict__})


class ReplayControl:
    """Open the Rockstar Editor for the user, and tidy up after it.

    action  "editor"   ACTIVATE_ROCKSTAR_EDITOR -- opens the Editor frontend
            "reset"    RESET_EDITOR_VALUES
            "fadein"   DO_SCREEN_FADE_IN(a ms) -- needed after leaving the Editor

    ⚠ ScriptHookV scripts are suspended for as long as the Editor is active
    (menus and playback alike), so the plugin can do nothing while it is up:
    no capture, no poses, no camera, no input. Everything the Editor does, a
    person does in the Editor; render_clip.py wraps that.
    """

    def __init__(self, action="editor", a=0.0, b=0.0, frames=0):
        self.action = str(action)
        self.a = float(a)
        self.b = float(b)
        self.frames = int(frames)

    def to_json(self):
        return json.dumps({'ReplayControl': self.__dict__})


class SetCapturePause:
    """Whether the plugin wraps each frame capture in SET_GAME_PAUSED.

    ⚠ The Rockstar Editor's replay recorder latches on that native: once the
    capture loop has paused the game even once, no clip saves for the rest of
    the game process ("Clips must be at least 3 seconds long", regardless of
    length). A run that records .clip files captures with SET_TIME_SCALE(0)
    alone. Default in the plugin is True (upstream behaviour).
    """

    def __init__(self, enabled=True):
        self.enabled = bool(enabled)

    def to_json(self):
        return json.dumps({'SetCapturePause': self.__dict__})


class SetClipRecording:
    """Drive the Rockstar Editor's recorder around a clip.

    action  "start"   begin recording (mode is the native's argument; the
                      client verifies via the per-frame `clip_recording` flag
                      rather than trusting the mode's documented meaning)
            "save"    stop and write the .clip into the Editor's library
            "discard" stop and throw it away

    The .clip lands in the Editor's own library directory, unnamed; the capture
    client matches it to the clip it belongs to by watching that directory.
    """

    def __init__(self, action="start", mode=1, control=-1, group=0, frames=3):
        self.action = str(action)
        self.mode = int(mode)
        # "press": hold input `control` of `group` at 1.0 for `frames` frames --
        # emulating a key, e.g. the Editor's own start/stop-recording shortcut.
        self.control = int(control)
        self.group = int(group)
        self.frames = int(frames)

    def to_json(self):
        return json.dumps({'SetClipRecording': self.__dict__})


class SetSceneDensity:
    """Ambient traffic / pedestrian density multipliers.

    ⚠ The underlying natives are all `*_THIS_FRAME`, so the plugin re-applies
    these every tick from Scenario::run(). A one-shot native call would appear
    to do nothing -- which looks like a broken native rather than a misuse.
    """

    def __init__(self, vehicle=1.0, randomVehicle=1.0, parkedVehicle=1.0,
                 ped=1.0, scenarioPed=1.0):
        self.vehicle = vehicle
        self.randomVehicle = randomVehicle
        self.parkedVehicle = parkedVehicle
        self.ped = ped
        self.scenarioPed = scenarioPed

    def to_json(self):
        return json.dumps({'SetSceneDensity': self.__dict__})


class SetCameraMount:
    """Where the POV camera sits on the ego, as fractions of its half-extent.

    ⚠ The default used to be 0.90 forward -- 90% of the way to the front bumper,
    i.e. mounted on the nose. Anything struck head-on passes UNDER the camera and
    is never seen, which makes the most informative moment of a pedestrian
    collision invisible. 0.35 is a windscreen position: bonnet and impact zone
    both in frame, and where a real dashcam sits.

    ⚠ Changing this changes the extrinsics, so clips captured either side of a
    change are not geometrically comparable. meta.json records the pose per frame,
    so the data stays usable -- but do not mix them without noticing.
    """

    def __init__(self, forwardFrac=0.35, upFrac=0.95):
        self.forwardFrac = float(forwardFrac)
        self.upFrac = float(upFrac)

    def to_json(self):
        return json.dumps({'SetCameraMount': self.__dict__})


class SeedScene:
    """Place traffic and VRUs around the ego rather than hoping the game spawns them.

    ★ Measured: damage to other vehicles scales ~4.7x with how many are in scene
    (mean 0.7 damaged when <=4 nearby, 3.3 when >=8), and the median clip had only
    4 vehicles within 60 m. Density was the binding constraint on chaos.

    ⚠ SET_VEHICLE_DENSITY_MULTIPLIER_THIS_FRAME cannot fix that -- it SCALES GTA's
    population model rather than inventing traffic, so 3x of a near-zero base is
    still near zero. Vehicles and cyclists are placed on vehicle NODES (legal
    position and heading); peds go through GET_SAFE_COORD_FOR_PED so they land on
    pavements rather than inside geometry.

    Send once per clip, after the ego has settled.
    """

    def __init__(self, vehicles=0, peds=0, cyclists=0, radius=120.0, seed=0,
                 clearAmbient=False):
        self.vehicles = int(vehicles)
        self.peds = int(peds)
        self.cyclists = int(cyclists)
        self.radius = float(radius)
        # seed: every model choice in the seeded population derives from it, so the
        # same scene gets the same people and cars. clearAmbient: remove the game's
        # own population first and hold its spawner at zero for the clip -- GTA
        # exposes no way to seed that spawner, so the only reproducible scene is
        # one where the seeded population IS the population.
        self.seed = int(seed) & 0xFFFFFFFF
        self.clearAmbient = bool(clearAmbient)

    def to_json(self):
        return json.dumps({'SeedScene': self.__dict__})


class SetIgniteWrecks:
    """Set a wrecked vehicle on fire once its body health falls below a threshold.

    ⚠ GTA only ignites a vehicle when its ENGINE health goes negative, which a
    single road collision does not do. Measured tank_health_min across a whole set
    of collisions: 984.9 of 1000, with on_fire false on every clip -- weakening the
    petrol tank does not produce fires. This ignites explicitly instead.
    """

    def __init__(self, enabled=True, belowHealth=0.0):
        self.enabled = bool(enabled)
        self.belowHealth = float(belowHealth)   # 0 = keep the plugin default

    def to_json(self):
        return json.dumps({'SetIgniteWrecks': self.__dict__})


class SetActorBehaviour:
    """How every non-ego road user behaves, settable per clip.

    ★ This is what makes counterfactual capture possible. Until it existed, every
    NPC knob lived as a private member inside the plugin with no message, so the
    build could only ever produce one flavour of traffic. Replaying the SAME scene
    with actors sane in one clip and reckless in the next needs all of it to come
    from the client, per clip.

    drivingStyle       GTA driving-style bitmask; see longtail.drivingstyles. NORMAL
                       (786603) obeys lights and lanes; 512|262144 is wrong-way +
                       shortest-path with no avoidance and no stopping.
    aggressiveness     0..1, stock traffic is ~0.0
    ability            0..1, stock traffic is ~1.0 -- LOWER is worse driving
    cruiseSpeed        m/s ceiling for NPC drivers
    steersAround       True = stock avoidance of vehicles/peds/objects; False =
                       drivers commit to contact
    trafficAggression  master switch for the re-tasking sweep
    pedInteraction     master switch for sending pedestrians across the road
    pedCrossChance     fraction of eligible peds sent across the ego's path
    """

    def __init__(self, drivingStyle=512 | 262144, aggressiveness=1.0, ability=0.0,
                 cruiseSpeed=40.0, steersAround=False, trafficAggression=True,
                 pedInteraction=True, pedCrossChance=0.35):
        self.drivingStyle = int(drivingStyle)
        self.aggressiveness = float(aggressiveness)
        self.ability = float(ability)
        self.cruiseSpeed = float(cruiseSpeed)
        self.steersAround = bool(steersAround)
        self.trafficAggression = bool(trafficAggression)
        self.pedInteraction = bool(pedInteraction)
        self.pedCrossChance = float(pedCrossChance)

    def to_json(self):
        return json.dumps({'SetActorBehaviour': self.__dict__})


class SetRoadLeash:
    """Keep the ego on the vehicle node network.

    The plugin checks the ego's distance to the nearest vehicle node a few times
    a second. Beyond `dist` for `seconds` continuously it clears the drive task
    and steers back to the node with a road-respecting style, restoring the
    clip's own style (minus IGNORE_ROADS / IGNORE_ALL_PATHING) once back on.

    ⚠ Node positions are on the carriageway *centreline*, so `dist` has to clear
    half the width of the widest road you care about -- 18 m, not 5 m. Turning
    the leash off also stops the plugin masking the off-network style bits.
    """

    def __init__(self, enabled=True, dist=0.0, seconds=0.0):
        self.enabled = bool(enabled)
        self.dist = float(dist)       # 0 = leave the plugin default
        self.seconds = float(seconds)  # 0 = leave the plugin default

    def to_json(self):
        return json.dumps({'SetRoadLeash': self.__dict__})


class SetTimeScale:
    """Time scale restored after each captured frame.

    Capture already pauses the game and sets scale 0 per frame; upstream restored
    a hard-coded 1.0. Dropping this to ~0.2 around a collision spends ~5x the
    frames on the highest-information half-second at the same in-game duration.
    ⚠ Costs proportionally more wall clock for that segment.
    """

    def __init__(self, scale=1.0):
        self.scale = scale

    def to_json(self):
        return json.dumps({'SetTimeScale': self.__dict__})


class PrepareLocation:
    """Pre-stream a location we have not travelled to yet.

    Send this during clip N for clip N+1. `NEW_LOAD_SCENE_START` is asynchronous,
    so the world around the next spawn point streams in while the current clip is
    still recording, and the relocate that follows pays almost nothing. This is
    what makes a ~1s warmup possible instead of 6-25s.
    """

    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = x
        self.y = y
        self.z = z

    def to_json(self):
        return json.dumps({'PrepareLocation': self.__dict__})
