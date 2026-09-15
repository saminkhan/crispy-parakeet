#!/usr/bin/env python3
"""Render a captured scene again from a Rockstar Editor .clip: new camera, new mp4.

    python3 render_clip.py --clip <dataset>/clips/<id>/clip.clip --out renders/<id>-left
    python3 render_clip.py --clip .../clip.clip --camera 0,-0.4,0.1,0,0,0 --out renders/left
    python3 render_clip.py --clip .../clip.clip --camera 0,0,1.2,-10,0,0 --out renders/roof

The .clip is the scene the capture run recorded with the game's own recorder --
entity states, not pixels. This tool puts it back where the Rockstar Editor
looks for it, starts the game with the capture plugin in RENDER mode, plays the
clip in the Editor, and captures it again: frames + poses from a camera on the
replayed ego, at the recorded seat mount plus whatever offset you ask for.

Output, in --out: clip.mp4 (true-speed, frame k == poses line k), poses.jsonl,
meta.json (the transform, the source clip, and provenance). The original
poses.jsonl beside the .clip is read when present -- not to produce anything, but
to lock the camera onto the right vehicle (the replayed ego is found where the
original trajectory began) and to report how far the re-render's trajectory
drifts from the original.

--camera is dx,dy,dz[,droll,dpitch,dyaw]: metres right/forward/up and degrees,
in the vehicle's frame, ADDED to the mount the clip was captured with. Omit it
for the original camera.
"""

import argparse
import json
import os
import shutil
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "VPilot"), os.path.join(_HERE, "VPilot", "longtail")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from capture import game as game_mod                      # noqa: E402
from capture.settings import CaptureSettings              # noqa: E402


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def parse_camera(spec):
    """'dx,dy,dz[,droll,dpitch,dyaw]' -> (pos xyz m, rot xyz deg), zeros if empty."""
    if not spec:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    parts = [float(v) for v in spec.replace(";", ",").split(",") if v.strip()]
    if len(parts) not in (3, 6):
        raise ValueError("--camera wants dx,dy,dz or dx,dy,dz,droll,dpitch,dyaw, got %r" % spec)
    pos = tuple(parts[:3])
    rot = tuple(parts[3:6]) if len(parts) == 6 else (0.0, 0.0, 0.0)
    return pos, rot


def resolve_inputs(clip_path, poses_path=None, meta_path=None):
    """Find the original poses.jsonl / meta.json next to the .clip when not given."""
    clip_path = os.path.abspath(clip_path)
    if not os.path.isfile(clip_path):
        raise ValueError("no such .clip: %s" % clip_path)
    folder = os.path.dirname(clip_path)
    if poses_path is None and os.path.isfile(os.path.join(folder, "poses.jsonl")):
        poses_path = os.path.join(folder, "poses.jsonl")
    if meta_path is None and os.path.isfile(os.path.join(folder, "meta.json")):
        meta_path = os.path.join(folder, "meta.json")
    meta = None
    if meta_path and os.path.isfile(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
    first_pose = None
    if poses_path and os.path.isfile(poses_path):
        with open(poses_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    first_pose = json.loads(line)
                    break
    return clip_path, poses_path, meta, first_pose


def editor_name_for(clip_path, meta):
    """The name the Editor knew the file by. The capture run records it; a bare
    .clip keeps its own name."""
    rc = (meta or {}).get("rockstar_clip") or {}
    name = rc.get("original_name")
    if name and name.lower().endswith(".clip"):
        return name
    base = os.path.basename(clip_path)
    if base.lower() == "clip.clip":
        # Give it a name shaped like the Editor's own so the library sorts it.
        return time.strftime("%b-%d-%Y-Clip-9999.clip")
    return base


def install_in_library(clip_path, library, name):
    """Copy the .clip (+ thumbnail) into the Editor's library under `name`.
    Returns the paths written, for removal afterwards."""
    os.makedirs(library, exist_ok=True)
    dst = os.path.join(library, name)
    shutil.copyfile(clip_path, dst)
    written = [dst]
    thumb = os.path.join(os.path.dirname(clip_path), "clip_thumb.jpg")
    if os.path.isfile(thumb):
        tdst = os.path.splitext(dst)[0] + ".jpg"
        shutil.copyfile(thumb, tdst)
        written.append(tdst)
    return written


# ---------------------------------------------------------------------------
# The render session
# ---------------------------------------------------------------------------

def render(args):
    from deepgtav.client import Client
    from deepgtav.messages import (Dataset, ReplayControl, SetCameraPositionAndRotation,
                                   SetCapturePause, SetRenderTarget, StartRecording,
                                   StartRender, Stop)
    import posemath
    import writer as writer_mod
    from config import CaptureConfig

    clip_path, poses_path, meta, first_pose = resolve_inputs(args.clip, args.poses, args.meta)
    cam_pos, cam_rot = parse_camera(args.camera)
    library = args.library or game_mod.clip_library_dir()
    if not library:
        raise ValueError("cannot find the Rockstar Editor library; pass --library")
    name = editor_name_for(clip_path, meta)

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    print("[render] clip      %s" % clip_path)
    print("[render] library   %s  (as %s)" % (library, name))
    print("[render] camera    +%s m, +%s deg on the recorded mount" % (cam_pos, cam_rot))
    print("[render] out       %s" % out)

    written = install_in_library(clip_path, library, name)
    try:
        # The game, with the plugin. Reuses a healthy one.
        settings = CaptureSettings(gta_dir=args.gta_dir, output_dir=out, width=args.width,
                                   height=args.height, rate_hz=args.rate, host=args.host,
                                   port=args.port, record_clip=False, make_mp4=True)
        g = game_mod.Game(settings)
        g.ensure_running()

        client = Client(ip=args.host, port=args.port, recv_timeout_ms=120000)
        # ⚠ Replay playback and SET_GAME_PAUSED do not mix any better than the
        # recorder and SET_GAME_PAUSED did. Time-scale-only capture, as in a run.
        client.sendMessage(SetCapturePause(enabled=False))
        client.sendMessage(StartRender(dataset=Dataset(
            rate=args.rate, speed=True, location=True, time=True,
            frame=[args.width, args.height], screenResolution=[args.width, args.height])))
        client.sendMessage(StartRecording())
        client.sendMessage(SetCameraPositionAndRotation(
            cam_pos[0], cam_pos[1], cam_pos[2], cam_rot[0], cam_rot[1], cam_rot[2]))

        cfg = CaptureConfig(width=args.width, height=args.height, rate_hz=args.rate,
                            out_dir=out, image_format="jpg", video_crf=args.crf,
                            video_only=not args.keep_frames)
        clip_id = args.clip_id or ("render-" + time.strftime("%Y%m%d-%H%M%S"))

        # Play it. See _drive_editor: this is the part that talks to the Editor's
        # menus, and it is the part that needs a human until it is finished.
        _drive_editor(client, name, args)

        # Lock the camera onto the replayed ego where the original run began.
        if first_pose and first_pose.get("position"):
            x, y, z = first_pose["position"]
            client.sendMessage(SetRenderTarget(x, y, z, args.lock_radius))
            print("[render] target   locking on the closest vehicle to (%.1f, %.1f, %.1f) within %.0f m"
                  % (x, y, z, args.lock_radius))
        else:
            print("[render] target   none (no original poses): rendering the replay's own camera")

        result = _capture_playback(client, cfg, clip_id, posemath, writer_mod, args)
        client.sendMessage(Stop())
        client.close()
    finally:
        for p in written:
            try:
                os.remove(p)
            except OSError:
                pass

    result.update({
        "source_clip": clip_path, "editor_name": name,
        "camera_offset": {"pos_m": list(cam_pos), "rot_deg": list(cam_rot)},
        "source_meta": (meta or {}).get("clip_id"),
    })
    if poses_path and result.get("poses"):
        result["drift_vs_original"] = _drift(poses_path, result["poses"], cam_pos)
    with open(os.path.join(out, "render.json"), "w") as f:
        json.dump(result, f, indent=2)
    print("[render] done      %s" % json.dumps({k: result[k] for k in ("frames", "duration_s", "mp4") if k in result}))
    return 0


def _drive_editor(client, name, args):
    """Get the Editor playing `name`.

    ⚠ There is no native that plays a given clip. What the plugin offers is
    ACTIVATE_ROCKSTAR_EDITOR and per-frame frontend input, so this walks the
    Editor's menus blind. Until that walk is proven on this build, --manual
    asks you to press play yourself, and the capture starts when the replayed
    ego appears.
    """
    from deepgtav.messages import ReplayControl
    if args.manual:
        print("[render] ⚠ MANUAL: in the game, open the Rockstar Editor, pick clip %s and "
              "press play. Capture starts when the ego appears." % name)
        return
    client.sendMessage(ReplayControl(action="editor"))
    raise NotImplementedError("automatic Editor navigation is not finished; run with --manual")


def _capture_playback(client, cfg, clip_id, posemath, writer_mod, args):
    """Receive frames until the target vanishes (replay over) or --max-seconds."""
    from deepgtav.messages import frame2numpy
    w = writer_mod.ClipWriter(cfg.out_dir, clip_id, cfg)
    t_start = time.monotonic()
    locked = False
    n = 0
    t0_game = None
    last_game = None
    while time.monotonic() - t_start < args.max_seconds:
        msg = client.recvMessage()
        if msg is None:
            continue
        if "CameraPosition" not in msg:
            continue
        state = posemath.ego_state_from_message(msg)
        if not locked:
            if state.get("render_target"):
                locked = True
                print("[render] target   locked (vehicle %d); recording" % state["render_target"])
            else:
                continue
        elif not state.get("render_target"):
            print("[render] target   gone; replay finished")
            break
        gt = state["game_time_ms"]
        if t0_game is None:
            t0_game = gt
        if last_game is not None and gt <= last_game:
            continue
        last_game = gt
        pose = posemath.pose_from_message(msg)
        img = frame2numpy(msg["frame"], (cfg.width, cfg.height)) if msg.get("frame") else None
        w.add_frame(img, pose, extra={"t": round((gt - t0_game) / 1000.0, 4), "speed": state.get("speed", 0.0)})
        n += 1
    w.finalize({"clip_id": clip_id, "keep": True, "reject_reasons": [], "render": True})
    duration = ((last_game - t0_game) / 1000.0) if (t0_game is not None and last_game is not None) else 0.0
    video = None
    if n >= 2:
        video = writer_mod.encode_video(w.dir, cfg, cfg.image_format)
    return {"frames": n, "duration_s": round(duration, 3),
            "mp4": os.path.join(w.dir, "clip.mp4") if video else None,
            "poses": os.path.join(w.dir, "poses.jsonl"), "clip_dir": w.dir,
            "video": video}


def _drift(original_poses, rendered_poses, cam_offset):
    """RMS distance between the rendered camera track and the original's, after
    removing the requested offset -- how faithfully the replay reproduced the
    scene. Time-aligned by index; a coarse but honest number."""
    import math
    def load(p):
        out = []
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line).get("position"))
        return [o for o in out if o]
    a, b = load(original_poses), load(rendered_poses)
    n = min(len(a), len(b))
    if n == 0:
        return None
    off = math.sqrt(sum(v * v for v in cam_offset))
    d = [math.dist(a[i], b[i]) for i in range(n)]
    return {"frames_compared": n, "rms_m": round(math.sqrt(sum(x * x for x in d) / n), 3),
            "max_m": round(max(d), 3), "requested_offset_m": round(off, 3)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clip", required=True, help="the .clip to render (clip.clip in a clip folder)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--camera", default="", metavar="dx,dy,dz[,droll,dpitch,dyaw]",
                    help="offset from the recorded mount: metres right,forward,up and degrees")
    ap.add_argument("--poses", default=None, help="original poses.jsonl (default: beside the .clip)")
    ap.add_argument("--meta", default=None, help="original meta.json (default: beside the .clip)")
    ap.add_argument("--gta-dir", required=True, help="folder containing GTA5.exe")
    ap.add_argument("--library", default=None, help="Rockstar Editor clip library (default: from the registry)")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--rate", type=int, default=30)
    ap.add_argument("--crf", type=int, default=16)
    ap.add_argument("--keep-frames", action="store_true")
    ap.add_argument("--host", default="172.28.32.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--clip-id", default=None)
    ap.add_argument("--lock-radius", type=float, default=25.0,
                    help="metres around the original start to look for the replayed ego")
    ap.add_argument("--max-seconds", type=float, default=120.0, help="give up after this long")
    ap.add_argument("--manual", action="store_true",
                    help="you press play in the Editor; the tool captures when the ego appears")
    args = ap.parse_args(argv)
    try:
        return render(args)
    except (ValueError, NotImplementedError) as exc:
        sys.stderr.write("[render] %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
