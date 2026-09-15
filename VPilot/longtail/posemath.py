"""Camera pose and intrinsics for GTA V frames.

Everything here is a deliberate port of what the plugin does in C++, not a
re-derivation. The rotation convention in particular is taken verbatim from
`DeepGTAV-PreSIL/ObjectDet/Functions.h::rotate`, because guessing GTA's Euler
convention is the classic way to produce a dataset where every frame looks
correct and no two frames agree.

GTA world axes:  +X = east, +Y = north, +Z = up  (right-handed, Z-up).
The plugin builds the camera basis by rotating those world axes:
    forward = rotate(WORLD_NORTH, theta)
    up      = rotate(WORLD_UP,    theta)
    right   = rotate(WORLD_EAST,  theta)
with theta = CAM::GET_CAM_ROT(camera, 0) in degrees.
"""

import numpy as np

WORLD_EAST = np.array([1.0, 0.0, 0.0])
WORLD_NORTH = np.array([0.0, 1.0, 0.0])
WORLD_UP = np.array([0.0, 0.0, 1.0])


def rotation_matrix(theta_deg):
    """R = Rz(tz) @ Ry(ty) @ Rx(tx), matching Functions.h::rotate exactly.

    Verified against the C++ expression term by term; see test_posemath.py.
    """
    tx, ty, tz = np.radians(np.asarray(theta_deg, dtype=np.float64))
    cx, sx = np.cos(tx), np.sin(tx)
    cy, sy = np.cos(ty), np.sin(ty)
    cz, sz = np.cos(tz), np.sin(tz)

    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return rz @ ry @ rx


def camera_basis(theta_deg):
    """Return (right, up, forward) unit vectors in world coordinates."""
    r = rotation_matrix(theta_deg)
    return r @ WORLD_EAST, r @ WORLD_UP, r @ WORLD_NORTH


def c2w_opencv(position, theta_deg):
    """4x4 camera-to-world for the OpenCV convention (x right, y down, z fwd)."""
    right, up, forward = camera_basis(theta_deg)
    m = np.eye(4)
    m[:3, 0] = right
    m[:3, 1] = -up          # OpenCV y points down
    m[:3, 2] = forward
    m[:3, 3] = np.asarray(position, dtype=np.float64)
    return m


def c2w_opengl(position, theta_deg):
    """4x4 camera-to-world for the OpenGL/NeRF convention (x right, y up, z back)."""
    right, up, forward = camera_basis(theta_deg)
    m = np.eye(4)
    m[:3, 0] = right
    m[:3, 1] = up
    m[:3, 2] = -forward     # OpenGL looks down -z
    m[:3, 3] = np.asarray(position, dtype=np.float64)
    return m


def w2c_opencv(position, theta_deg):
    """4x4 world-to-camera (OpenCV). X_cam = w2c @ [X_world, 1]."""
    return np.linalg.inv(c2w_opencv(position, theta_deg))


def intrinsics(fov_deg_vertical, width, height, aspect_ratio=None):
    """Pinhole K from the plugin's reported vertical FOV.

    The plugin computes its near-plane extents as
        ncHeight = 2 * near * tan(fov/2)
        ncWidth  = ncHeight * GRAPHICS::_GET_SCREEN_ASPECT_RATIO(false)
    so the horizontal scale is set by the *reported* aspect ratio, which is NOT
    necessarily width/height.

        fy = height / (2 tan(fov/2))
        fx = width  / (2 tan(fov/2) * aspect)

    They are only equal when aspect == width/height. That is worth caring about:
    this fork runs happily in DSR modes where the captured buffer resolution and
    the screen resolution differ (see utils/Constants.py), and in those modes
    assuming square pixels silently shears every reconstruction.

    Passing aspect_ratio=None falls back to width/height, i.e. square pixels.
    """
    if aspect_ratio is None or aspect_ratio <= 0:
        aspect_ratio = float(width) / float(height)
    t = np.tan(np.radians(float(fov_deg_vertical)) / 2.0)
    fy = height / (2.0 * t)
    fx = width / (2.0 * t * aspect_ratio)
    return np.array([[fx, 0.0, width / 2.0],
                     [0.0, fy, height / 2.0],
                     [0.0, 0.0, 1.0]])


def project(points_world, position, theta_deg, k):
    """Project Nx3 world points to Nx2 pixels (OpenCV convention).

    Returns (uv, z_cam). Points with z_cam <= 0 are behind the camera.
    """
    pts = np.atleast_2d(np.asarray(points_world, dtype=np.float64))
    hom = np.hstack([pts, np.ones((pts.shape[0], 1))])
    cam = (w2c_opencv(position, theta_deg) @ hom.T).T[:, :3]
    z = cam[:, 2]
    with np.errstate(divide='ignore', invalid='ignore'):
        uv = (k @ (cam / z[:, None]).T).T[:, :2]
    return uv, z


def pose_from_message(msg):
    """Pull the fields the patched plugin emits into a pose dict.

    Requires the `longtail` C++ patches for CameraFOV / CameraAspectRatio /
    GameTime. Note that upstream's `focalLen` field is declared and never
    assigned -- it is always 0.0 -- so it is deliberately ignored here.
    """
    return {
        "position": list(map(float, msg["CameraPosition"])),
        "theta_deg": list(map(float, msg["CameraAngle"])),
        "fov_deg": float(msg.get("CameraFOV", 0.0)),
        "aspect_ratio": float(msg.get("CameraAspectRatio", 0.0)),
        "near_clip": float(msg.get("CameraNearClip", 0.0)),
        "far_clip": float(msg.get("CameraFarClip", 0.0)),
        "game_time_ms": int(msg.get("GameTime", 0)),
    }


def ego_state_from_message(msg):
    """Per-frame ego state used for outcome labelling and warmup gating."""
    def f(k, d=0.0):
        v = msg.get(k, d)
        return d if v is None else float(v)

    def b(k):
        return bool(msg.get(k, False))

    return {
        "game_time_ms": int(msg.get("GameTime", 0)),
        "speed": f("EgoSpeed"),
        "collided": b("EgoCollided"),
        "on_fire": b("EgoOnFire"),
        "health": f("EgoHealth"),
        "engine_health": f("EgoEngineHealth", 1000.0),
        "body_health": f("EgoBodyHealth", 1000.0),
        "tank_health": f("EgoTankHealth", 1000.0),
        "roll": f("EgoRoll"),
        "pitch": f("EgoPitch"),
        "on_roof": b("EgoOnRoof"),
        "ped_health": f("EgoPedHealth"),
        "ped_in_vehicle": bool(msg.get("EgoPedInVehicle", True)),
        "screen_faded": b("ScreenFaded"),
        "world_ready": b("WorldReady"),
        "scene_streamed": b("SceneStreamed"),
        "road_node_valid": b("RoadNodeValid"),
        "road_node_density": int(msg.get("RoadNodeDensity") or 0),
        "road_node_flags": int(msg.get("RoadNodeFlags") or 0),
        # ⚠ road_node_valid stays True well after the car has left the
        # carriageway. Use the distance for any "is the ego off-road" test.
        "road_node_dist": f("RoadNodeDist"),
        # Advances on every scenario build; the exact signal that a relocate happened.
        "scenario_gen": int(msg.get("ScenarioGen") or 0),
        # A lawful ego stops at red lights; that is not a stuck spawn.
        "at_traffic_light": b("EgoAtLight"),
        "clip_recording": b("ClipRecording"),
        # [rockstar] Editor / replay state for render_clip.py
        "pause_menu": b("PauseMenuActive"),
        "replay_script_refs": int(f("ReplayScriptRefs") or 0),
        "player_ped": b("PlayerPedExists"),
        "player_in_vehicle": b("PlayerInVehicle"),
        "render_mode": b("RenderMode"),
        "render_target": int(f("RenderTarget") or 0),
        "screen_faded_out": b("ScreenFadedOut"),
        "gameplay_cam_pos": msg.get("GameplayCamPos"),
        "gameplay_cam_rot": msg.get("GameplayCamRot"),
        # ★ What the OTHER vehicles are doing. Every other measurement is about
        # the ego, so "do collisions displace and damage other traffic" could only
        # be judged by watching clips.
        "nearby_vehicles": int(msg.get("NearbyVehicles") or 0),
        "nearby_damaged": int(msg.get("NearbyDamaged") or 0),
        "nearby_on_fire": int(msg.get("NearbyOnFire") or 0),
        "nearby_wrecked": int(msg.get("NearbyWrecked") or 0),
        "nearby_max_speed": f("NearbyMaxSpeed"),
        "nearby_min_body_health": f("NearbyMinBodyHealth", 1000.0),
        # Identified so a change of subject is distinguishable from a jump.
        "nearest_veh_id": int(msg.get("NearestVehId") or 0),
        "nearest_veh_pos": list(msg.get("NearestVehPos") or [0.0, 0.0, 0.0]),
        # ★ The REAL backbuffer. If it disagrees with the requested frame size the
        # frames are resampled and K is a fiction -- GTA V is DPI-unaware, so on a
        # scaled display it renders smaller than settings.xml claims and nothing
        # reports it.
        "backbuffer_w": int(msg.get("BackbufferWidth") or 0),
        "backbuffer_h": int(msg.get("BackbufferHeight") or 0),
    }
