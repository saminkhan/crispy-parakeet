#!/usr/bin/env python3
"""Render a captured scene again from its Rockstar Editor .clip: new video, new poses.

    # 1. put the clip(s) where the Editor looks, start the game, open the Editor
    python3 render_clip.py stage --gta-dir "D:/.../Grand Theft Auto V" \\
        <dataset>/clips/<id>/clip.clip [<more clip.clip files>]

    # 2. in the Editor: new project -> add the clip -> camera -> export (see below)

    # 3. collect the exported video and write poses for it
    python3 render_clip.py collect --clip <dataset>/clips/<id>/clip.clip \\
        --camera 0,-0.4,0.1 --out renders/<id>-left

The .clip is the scene the capture run recorded with the game's own recorder --
entity states, not pixels. The Rockstar Editor replays it in-engine and can
export it as a video at any resolution and framerate, from any camera. What this
tool does is everything around that export:

  stage    copies the .clip (and its thumbnail) into the Editor's library under
           the name the Editor gave it, restarts the game so the Editor's clip
           list picks it up (⚠ the list is built when the game boots), and opens
           the Editor.
  collect  takes the video the Editor exported (videos/rendered, newest by
           default), copies it next to the clip as clip.mp4 in --out, and writes
           a poses.jsonl for it: the original pose track resampled to the video's
           frame times, with the camera offset you rendered with applied. So
           frame k of the exported video has line k in poses.jsonl, the way a
           captured clip does.
  unstage  removes staged clips from the library again.

⚠ The middle step is a person in the Editor. ScriptHookV scripts are suspended
for as long as the Rockstar Editor is active -- its menus and its playback -- so
the capture plugin can neither play a clip nor read anything while one plays.
(A hook-based approach, as REPlus takes, is the way past that; it is not in this
tool.) The manual part, once per render:

  Rockstar Editor -> Create New Project -> Add Clip -> pick the staged clip ->
  Edit Clip -> Camera -> Free Camera -> attach it to the ego vehicle and set the
  position/rotation offsets -> Save -> Export (choose resolution and framerate).

⚠ The poses collect writes describe the CAPTURE MOUNT (the seat-bone camera the
run recorded with), plus --camera. They only match the exported pixels if the
Editor's camera reproduces that mount -- which is why the camera has to be a
Free Camera attached to the ego, not "Game Camera": the game camera in a .clip
is the third-person gameplay view the replay recorded, whose pose this tool
cannot produce. --camera is dx,dy,dz[,droll,dpitch,dyaw]: metres right/forward/up
and degrees, ADDED to the mount, in the camera's own frame. The Editor's attach
offsets are expressed relative to the vehicle, ours relative to the recorded
camera; a one-time calibration render (same clip, known Editor offsets, compare
against the original poses) is how to find the fixed difference on your build --
collect reports every number it used so that comparison is possible.
"""

import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.join(_HERE, "VPilot"), os.path.join(_HERE, "VPilot", "longtail")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from capture import game as game_mod                      # noqa: E402
from capture.settings import CaptureSettings              # noqa: E402


def _client():
    """Import the ZeroMQ client and messages. ⚠ deepgtav.messages pulls in
    utils.PedNamesAndHashes, which opens utils/pedsToHashes.txt relative to the
    CURRENT DIRECTORY -- an upstream quirk the runner sidesteps by running the
    generator from VPilot/. Do the import with that cwd, then put it back."""
    cwd = os.getcwd()
    os.chdir(os.path.join(_HERE, "VPilot"))
    try:
        from deepgtav.client import Client
        from deepgtav.messages import ReplayControl
        return Client, ReplayControl
    finally:
        os.chdir(cwd)


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
    """The .clip plus the poses.jsonl / meta.json beside it (or given)."""
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
    return clip_path, poses_path, meta


def editor_name_for(clip_path, meta):
    """The name the Editor knew the file by. The capture run records it; a bare
    .clip keeps its own name; a nameless clip.clip gets an Editor-shaped one."""
    rc = (meta or {}).get("rockstar_clip") or {}
    name = rc.get("original_name")
    if name and name.lower().endswith(".clip"):
        return name
    base = os.path.basename(clip_path)
    if base.lower() == "clip.clip":
        return time.strftime("%b-%d-%Y-Clip-9%H%M.clip")
    return base


def library_dir(explicit=None):
    lib = explicit or game_mod.clip_library_dir()
    if not lib:
        raise ValueError("cannot find the Rockstar Editor library; pass --library")
    return lib


def rendered_dir(library):
    """videos/rendered, beside videos/clips: where the Editor writes exports."""
    return os.path.join(os.path.dirname(os.path.normpath(library)), "rendered")


# ---------------------------------------------------------------------------
# stage / unstage
# ---------------------------------------------------------------------------

def cmd_stage(args):
    library = library_dir(args.library)
    os.makedirs(library, exist_ok=True)
    staged = []
    for clip in args.clips:
        clip_path, _, meta = resolve_inputs(clip)
        name = editor_name_for(clip_path, meta)
        dst = os.path.join(library, name)
        shutil.copyfile(clip_path, dst)
        thumb = os.path.join(os.path.dirname(clip_path), "clip_thumb.jpg")
        if os.path.isfile(thumb):
            shutil.copyfile(thumb, os.path.splitext(dst)[0] + ".jpg")
        staged.append((clip_path, name))
        print("[stage] %s -> %s" % (clip_path, dst))
    with open(os.path.join(library, ".staged.json"), "w") as f:
        json.dump([{"clip": c, "name": n} for c, n in staged], f, indent=1)

    if args.no_launch:
        print("[stage] ⚠ the Editor lists clips it saw at boot: restart the game before "
              "opening the Editor")
        return 0
    # ⚠ Restart, not reuse: the clip list is built when the game boots.
    settings = CaptureSettings(gta_dir=args.gta_dir, output_dir=os.getcwd(),
                               host=args.host, port=args.port, record_clip=False, make_mp4=True)
    g = game_mod.Game(settings)
    print("[stage] restarting the game so the Editor sees the staged clip(s)")
    g.stop()
    g.ensure_running(force=True)
    if not args.no_editor:
        Client, ReplayControl = _client()
        c = Client(ip=args.host, port=args.port, recv_timeout_ms=5000)
        c.sendMessage(ReplayControl(action="editor"))
        c.close()
        print("[stage] Rockstar Editor opened")
    print("[stage] now in the Editor: Create New Project -> Add Clip -> %s -> camera -> Export"
          % ", ".join(n for _, n in staged))
    print("[stage] then: render_clip.py collect --clip <clip.clip> --out <dir> [--camera ...]")
    return 0


def cmd_unstage(args):
    library = library_dir(args.library)
    index = os.path.join(library, ".staged.json")
    names = []
    if os.path.isfile(index):
        with open(index) as f:
            names = [e["name"] for e in json.load(f)]
    for name in names:
        for p in (os.path.join(library, name), os.path.join(library, os.path.splitext(name)[0] + ".jpg")):
            try:
                os.remove(p)
                print("[unstage] removed %s" % p)
            except OSError:
                pass
    try:
        os.remove(index)
    except OSError:
        pass
    return 0


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

def _ffprobe(video):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate,nb_frames,width,height:format=duration",
         "-of", "json", video], capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    st = j["streams"][0]
    num, den = st["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    n = int(st.get("nb_frames") or 0)
    dur = float(j["format"].get("duration") or 0.0)
    if n <= 0 and dur > 0:
        n = int(round(dur * fps))
    return {"fps": fps, "frames": n, "duration_s": dur, "width": int(st["width"]), "height": int(st["height"])}


def _load_track(poses_path):
    rows = []
    with open(poses_path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows = [r for r in rows if "position" in r and "theta_deg" in r and "game_time_ms" in r]
    if len(rows) < 2:
        raise ValueError("original poses.jsonl has fewer than 2 usable rows")
    return rows


def _lerp_angle(a, b, u):
    d = ((b - a + 180.0) % 360.0) - 180.0
    return a + d * u


def _sample(rows, t_ms):
    """Pose at game time t_ms (relative to row 0), linear in position, shortest
    arc per Euler component. Clamped at the ends."""
    t0 = rows[0]["game_time_ms"]
    ts = [r["game_time_ms"] - t0 for r in rows]
    if t_ms <= ts[0]:
        r = rows[0]
        return list(r["position"]), list(r["theta_deg"]), float(r.get("fov_deg", 0.0))
    if t_ms >= ts[-1]:
        r = rows[-1]
        return list(r["position"]), list(r["theta_deg"]), float(r.get("fov_deg", 0.0))
    lo, hi = 0, len(ts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ts[mid] <= t_ms:
            lo = mid
        else:
            hi = mid
    a, b = rows[lo], rows[hi]
    u = (t_ms - ts[lo]) / float(max(1, ts[hi] - ts[lo]))
    pos = [a["position"][i] + (b["position"][i] - a["position"][i]) * u for i in range(3)]
    rot = [_lerp_angle(a["theta_deg"][i], b["theta_deg"][i], u) for i in range(3)]
    fov = float(a.get("fov_deg", 0.0))
    return pos, rot, fov


def _apply_offset(pos, theta, cam_pos, cam_rot):
    """Move the camera by cam_pos (right, forward, up, metres) in its own frame and
    turn it by cam_rot (degrees, added per component)."""
    import posemath
    right, up, fwd = posemath.camera_basis(theta)
    p = [pos[i] + right[i] * cam_pos[0] + fwd[i] * cam_pos[1] + up[i] * cam_pos[2] for i in range(3)]
    r = [theta[i] + cam_rot[i] for i in range(3)]
    return p, r


def cmd_collect(args):
    clip_path, poses_path, meta = resolve_inputs(args.clip, args.poses, args.meta)
    if not poses_path:
        raise ValueError("the original poses.jsonl is needed to write poses for the render; "
                         "pass --poses")
    cam_pos, cam_rot = parse_camera(args.camera)
    library = library_dir(args.library)

    video = args.video
    if not video:
        cands = sorted(glob.glob(os.path.join(rendered_dir(library), "*.mp4")), key=os.path.getmtime)
        if not cands:
            raise ValueError("no exported video found in %s; pass --video" % rendered_dir(library))
        video = cands[-1]
    info = _ffprobe(video)
    if info["frames"] < 2:
        raise ValueError("could not read a frame count from %s" % video)

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    dst_mp4 = os.path.join(out, "clip.mp4")
    shutil.copyfile(video, dst_mp4)

    rows = _load_track(poses_path)
    rc = (meta or {}).get("rockstar_clip") or {}
    # The .clip's t=0 sits offset_ms after poses.jsonl line 0 (recorded at capture).
    origin_ms = float(args.origin_ms if args.origin_ms is not None else rc.get("offset_ms", 0) or 0)
    fps = args.fps or info["fps"]
    n = info["frames"]
    with open(os.path.join(out, "poses.jsonl"), "w") as f:
        for k in range(n):
            t_ms = origin_ms + k * 1000.0 / fps
            pos, theta, fov = _sample(rows, t_ms)
            pos, theta = _apply_offset(pos, theta, cam_pos, cam_rot)
            f.write(json.dumps({
                "i": k, "file": None, "t": round(k / fps, 4),
                "game_time_ms": int(round(rows[0]["game_time_ms"] + t_ms)),
                "position": [round(v, 4) for v in pos],
                "theta_deg": [round(v, 4) for v in theta],
                "fov_deg": fov,
            }) + "\n")

    render_meta = {
        "source_clip": clip_path,
        "source_clip_id": (meta or {}).get("clip_id"),
        "source_poses": poses_path,
        "exported_video": video,
        "video": {"fps": fps, "frames": n, "width": info["width"], "height": info["height"],
                  "duration_s": info["duration_s"]},
        "clip_origin_offset_ms": origin_ms,
        "camera_offset": {"pos_m_right_forward_up": list(cam_pos), "rot_deg": list(cam_rot),
                          "frame": "recorded capture camera (see render_clip.py docstring)"},
        "poses_note": "original track resampled to the video's frame times; frame k == line k; "
                      "positions and rotations carry the camera offset, intrinsics do not "
                      "(re-derive from the export's resolution and the Editor's FOV)",
        "collected_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(os.path.join(out, "render.json"), "w") as f:
        json.dump(render_meta, f, indent=2)
    print("[collect] %s -> %s  (%d frames @ %.3f fps, %dx%d)" % (
        os.path.basename(video), dst_mp4, n, fps, info["width"], info["height"]))
    print("[collect] poses.jsonl: %d lines, origin +%.0f ms, offset %s m / %s deg" % (
        n, origin_ms, list(cam_pos), list(cam_rot)))
    return 0


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stage", help="put clip(s) in the Editor's library, restart the game, open the Editor")
    s.add_argument("clips", nargs="+", help="clip.clip file(s)")
    s.add_argument("--gta-dir", required=True, help="folder containing GTA5.exe")
    s.add_argument("--library", default=None, help="Rockstar Editor clip library (default: from the registry)")
    s.add_argument("--host", default="172.28.32.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-launch", action="store_true", help="only copy; do not restart the game")
    s.add_argument("--no-editor", action="store_true", help="restart the game but do not open the Editor")
    s.set_defaults(fn=cmd_stage)

    u = sub.add_parser("unstage", help="remove staged clips from the library")
    u.add_argument("--library", default=None)
    u.set_defaults(fn=cmd_unstage)

    c = sub.add_parser("collect", help="take the Editor's export and write poses for it")
    c.add_argument("--clip", required=True, help="the clip.clip the video was rendered from")
    c.add_argument("--out", required=True, help="output directory (clip.mp4, poses.jsonl, render.json)")
    c.add_argument("--video", default=None, help="the exported mp4 (default: newest in videos/rendered)")
    c.add_argument("--camera", default="", metavar="dx,dy,dz[,droll,dpitch,dyaw]",
                   help="the offset you rendered with, relative to the recorded mount")
    c.add_argument("--poses", default=None, help="original poses.jsonl (default: beside the .clip)")
    c.add_argument("--meta", default=None, help="original meta.json (default: beside the .clip)")
    c.add_argument("--library", default=None)
    c.add_argument("--fps", type=float, default=None, help="override the video's frame rate")
    c.add_argument("--origin-ms", type=float, default=None,
                   help="override the .clip's start relative to poses line 0 (default: from meta)")
    c.set_defaults(fn=cmd_collect)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (ValueError, subprocess.CalledProcessError) as exc:
        sys.stderr.write("[render] %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
