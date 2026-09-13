# Long-tail clip generator — setup and operation

Fork of [David0tt/DeepGTAV](https://github.com/David0tt/DeepGTAV) prepared for one job:
**generate long-tail / catastrophic driving clips from the ego perspective, with per-frame
camera extrinsics and intrinsics, for world-model latent training.**

No ground truth. No segmentation, no LiDAR, no bounding boxes. What matters here is
**geometric consistency** and **scene variation**.

---

## 0. What was changed and why

### The core problem with stock DeepGTAV

The upstream `Scenario::run()` applies this every 10 seconds, unconditionally:

```cpp
PLAYER::SET_EVERYONE_IGNORE_PLAYER(player, TRUE);      // NPCs stop reacting to you
ENTITY::SET_ENTITY_INVINCIBLE(m_ownVehicle, TRUE);     // no crashes
ENTITY::SET_ENTITY_PROOFS(m_ownVehicle, 1,1,1,1,1,1,1,1);  // no fire, no explosions
VEHICLE::SET_VEHICLE_CAN_BE_VISIBLY_DAMAGED(m_ownVehicle, FALSE);  // no damage
PED::SET_DRIVER_AGGRESSIVENESS(ped, 0.0);              // maximally defensive driver
```

This block is inherited verbatim from the 2017 Waterloo DeepGTAV fork. It is not neutral
about the long tail — **it is an explicit long-tail suppressor**, and it is why stock
DeepGTAV cannot capture any of the events you want. `SET_EVERYONE_IGNORE_PLAYER` in
particular is the one that kills reactive traffic: *"other drivers slam on the brakes"*
requires NPCs to react to the ego, and that flag turns it off.

### Changes on this branch (`longtail`)

| Area | Change |
|---|---|
| `Scenario.cpp/.h` | The guard block is now driven by a `SurvivalMode` struct via the new `SetSurvivalMode` message. Default capture preset: **player survives, car does not, everyone reacts.** |
| `Scenario.cpp/.h` | `createVehicle` gains `speed` / `drivingMode` / `actorId`. Upstream hard-coded `TASK_VEHICLE_DRIVE_WANDER(ped, veh, 2.0f, 16777216)`, so every spawned car crawled — useless for cut-ins or oncoming traffic. |
| `Scenario.cpp/.h` | New: `taskVehicleTempAction` (swerve / brake), `taskVehicleDriveToCoord`, `setEgoDrivingMode`, `resolveActor`. |
| `DataExport.cpp/.h` | New per-frame fields **`CameraFOV`, `CameraNearClip`, `CameraFarClip`, `CameraAspectRatio`, `GameTime`**. ⚠ Upstream declares `focalLen` in the message and **never assigns it** — it ships as `0.0`. Do not use it. |
| `Server.cpp` | Dispatch for the four new messages; `CreateVehicle`'s new fields are optional so existing scripts still work. |
| `VPilot/deepgtav/messages.py` | `SetSurvivalMode`, `TaskVehicleTempAction`, `TaskVehicleDriveToCoord`, `SetEgoDrivingMode`. |
| `VPilot/longtail/` | New generator package (below). |
| `Scenario.cpp/.h` | **Game-over guards + POV integrity**, enforced every frame (§0b). |
| `Scenario.cpp/.h` | `SetSceneDensity`, `SetTimeScale`, `PrepareLocation`. Ambient density had **zero** natives in the codebase before this. |
| `Scenario.cpp` | Spawn now sets `SET_VEHICLE_FORWARD_SPEED` + blocks on collision streaming, which is what removes the warmup (§0c). |
| `Scenario.cpp` | ⚠ Fixed an inverted null check that discarded the client's ego speed (`if (drivingMode[1].IsNull())`, missing `!`). |
| `DataExport.cpp/.h` | Per-frame **ego state** (health, damage, fire, roll, collision, in-vehicle, screen-faded) and **road context**; `WorldReady`/`SceneStreamed` for warmup gating. `SET_TIME_SCALE` restore is now a variable, not a hard-coded `1.0f`. |

### 0b. The game-over guarantee

`Scenario::enforceGameOverGuards()` runs **every frame**, independent of
`SurvivalMode`. These are not knobs. The failure modes blocked, in the order they
actually bite:

| Failure | Blocked by |
|---|---|
| Wasted screen / hospital respawn | `SET_PLAYER_INVINCIBLE`, `SET_ENTITY_INVINCIBLE(ped)`, `SET_ENTITY_PROOFS(ped,…)`, `SET_PED_SUFFERS_CRITICAL_HITS(FALSE)`, per-frame health top-up |
| Busted / arrest | `SET_POLICE_IGNORE_PLAYER`, `SET_MAX_WANTED_LEVEL(0)`, `SET_WANTED_LEVEL_MULTIPLIER(0)`, `SET_CREATE_RANDOM_COPS(FALSE)`, `RESET_PLAYER_ARREST_STATE` |
| Ejection through the windscreen | config flag 32, `SET_PED_CAN_RAGDOLL(FALSE)` |
| Dragged out by an NPC | `SET_PED_CAN_BE_DRAGGED_OUT(FALSE)` |
| Knocked off a bike | `SET_PED_CAN_BE_KNOCKED_OFF_VEHICLE(1)` |
| Fade-to-black on any restart | `SET_FADE_OUT_AFTER_DEATH/ARREST`, `SET_FADE_IN_AFTER_DEATH_ARREST`, `SET_FADE_IN_AFTER_LOAD`, `IGNORE_NEXT_RESTART`, `DISABLE_HOSPITAL_RESTART`, `DISABLE_POLICE_RESTART` |
| A fade that still slips through | detected and cancelled with `DO_SCREEN_FADE_IN(0)` the frame it appears |

**POV integrity.** `Scenario::enforceEgoIntegrity()` re-seats the ped if it ever
leaves the vehicle, rebuilds the scripted camera if it is destroyed, and
re-asserts `SET_CAM_ACTIVE` + `RENDER_SCRIPT_CAMS` every frame. The camera is
positioned from the vehicle matrix each capture, so the POV stays welded to the
front of the ego vehicle for the whole clip.

★ **Defence in depth.** The runner *also* aborts a clip the moment it observes
`ScreenFaded` or `EgoPedInVehicle == false`. A guard regression therefore shows
up as a dropped clip with a named reason, never as a black frame or a detached
camera in the training data. Both paths are covered by
`test_episode_offline.py`.

### 0c. Warmup removal

Warmup was 6–25 s. It is now **gate-based and typically well under a second**.
For a 5 s clip a 20 s warmup means 80% of the run is warmup, so this is the
single biggest throughput change.

The three things warmup used to buy, handled directly instead:

| Was waited for | Now |
|---|---|
| Ego accelerating to cruise (5–8 s) | `SET_VEHICLE_FORWARD_SPEED` at spawn — **instant** |
| Collision streaming | `PrepareLocation` for clip *N+1* is sent mid-clip *N* (`NEW_LOAD_SCENE_START` is async), so it is already resolved on arrival. Then gated on `HAS_COLLISION_LOADED_AROUND_ENTITY` |
| Ambient traffic populating | Event participants are **explicitly spawned** (instant). Ambient traffic is background dressing; `SET_ALL_LOW_PRIORITY_VEHICLE_GENERATORS_ACTIVE` + density multipliers fill it faster |

⚠ **It cannot go to zero, and you should not try.** The collision gate is the one
part that is load-bearing: without it the car falls through the world. It is
cheap once pipelined, so the honest floor is "one gate check", not "no wait".

⚠ **Visual LOD resolution is the part that is genuinely un-eliminable.** The
answer is economic rather than technical: warmup is a *fixed* cost on every clip,
whereas the pop-in reject gate is a *variable* cost on the fraction that fail. At
5 s clips, even a 40% rejection rate beats a 20 s warmup by a wide margin. Tune
`require_scene_streamed` if you would rather pay a little warmup to reject less;
it is worth measuring both ways on your hardware.

> ⚠ **The C++ has not been compiled.** It was written on Linux against the checked-in
> sources; building needs VS2022 on Windows. Expect to fix a signature or an include.
> The Python side is tested (`longtail/test_posemath.py` passes).

### What is deliberately *not* changed

`SET_PLAYER_INVINCIBLE` stays on, and the seatbelt flag (`SET_PED_CONFIG_FLAG(ped, 32, FALSE)`)
stays on. The split matters:

- **Player invincible** → the car can be destroyed without ever triggering the death /
  hospital cutscene, which would take the camera away mid-clip.
- **Seatbelt on** → the ped is not ejected through the windscreen, so the ego loop stays
  attached to the vehicle.
- **Police ignore on** → no chase, no wanted-level state machine.

Net effect: **the car crashes, deforms and burns; the player never dies and the police
never come.**

---

## 1. Prerequisites

- **GTA V**, current version. Tested upstream against Epic `1.0.3586.0` and newer.
  (Note: the original `aitorzip/DeepGTAV` requires ≤ `1.0.1180.2` and is therefore
  unusable on a modern install — that is the main reason to be on this fork.)
- **Visual Studio Community 2022** with **Desktop development with C++** *and*
  **Game Development with C++** selected. ⚠ Both. Without them DeepGTAV crashes with no
  error message.
- **Visual C++ Redistributable for Visual Studio 2017.**
- **ScriptHookV**, newest version, from <http://dev-c.com/GTAV/scripthookv>.
- A GPU that can hold a stable frame rate at your capture resolution. Disk: budget
  roughly **1080p PNG ≈ 2–3 MB/frame**; at 20 Hz that is ~3 GB/minute of kept clip.
  Use `image_format: "jpg"` if that is not acceptable.

---

## 2. Game settings — these are load-bearing

You are deriving a **pinhole** camera model from this renderer. Anything that violates
pinhole has to be off, or your intrinsics are a fiction.

1. **Windowed mode.**
2. **MSAA off.**
3. ⚠ **Depth of field, motion blur, and any post-process distortion off.** Motion blur
   especially: your most valuable frames are the high-angular-rate ones during a crash,
   and blur is exactly where pose/pixel agreement matters most.
4. **Extended distance scaling / LOD distance: high.** This is not cosmetic — it is the
   main defence against LOD pop-in, which is geometric inconsistency that *looks like
   plausible geometry* to a training loss.
5. Set `MANAGED_SCREEN_RESOLUTION` in `VPilot/utils/Constants.py` to your actual game
   resolution (e.g. `"1920x1080"`). Default is a 4K DSR mode.
6. ⚠ **Do not change FOV mid-run.** The plugin caches `s_camParams` behind an `init`
   flag; the `longtail` patch re-reads FOV per frame so `CameraFOV` stays correct, but
   several downstream assumptions in the writer take K as clip-constant.
7. ★★ **Make GTA V DPI-aware, or your backbuffer is not the resolution you asked for.**
   GTA V is DPI-unaware. On a display with Windows scaling above 100% it renders into a
   *virtualised* surface and the client area is the logical size, not the physical one.
   Measured here: a 2560x1440 panel at 150% scaling gave a **1708x960** backbuffer while
   `settings.xml` said 1920x1080 and the window measured 1920x1080.

   ⚠ Nothing reports this. The plugin resamples the backbuffer up to the requested frame
   size, so the stream is the right shape and the frames look fine — they are just
   upsampled from a smaller render, and `meta.json`'s K describes a sampling grid that
   does not exist. It also cost **45 ms per frame** in a scalar per-pixel resample loop
   and inflated every payload to 6.2 MB.

   Check it: the plugin logs the real backbuffer once per launch.

   ```
   grep backbuffer "<GTA V dir>/DeepGTAV.log"
   [longtail] backbuffer 1920x1080 fmt=87 samples=1 (target 1920x1080)   <- correct
   [longtail] backbuffer 1708x960  fmt=87 samples=1 (target 1920x1080)   <- DPI-virtualised
   ```

   Fix (per-user, reversible — the same thing as the exe's Compatibility tab →
   *Change high DPI settings* → *Override high DPI scaling behaviour: Application*):

   ```powershell
   $k='HKCU:\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers'
   Set-ItemProperty -Path $k -Name '<GTA V dir>\GTA5.exe'   -Value '~ HIGHDPIAWARE'
   Set-ItemProperty -Path $k -Name '<GTA V dir>\PlayGTAV.exe' -Value '~ HIGHDPIAWARE'
   ```

   ⚠ Keep `Windowed=1`, not borderless. Borderless on a DPI-aware app takes the whole
   panel (2560x1440 here), which is 1.8x the pixels to read back per frame for no gain.
8. **VSync off** (`<VSync value="0"/>`). `Present` is what the capture waits on; there is
   no reason to also block it on the refresh interval.


### The frame/pose contract

★ **`clip.mp4` frame *k* is `poses.jsonl` line *k*.** With `video_only` the JPEGs
are deleted after encoding, so the mp4 *is* the dataset and `poses.jsonl` is the
only description of what each of its frames shows. If the encoder ever drops,
duplicates or reorders a frame, every pose after that point describes the wrong
image, and nothing else in the pipeline would notice.

This is enforced, not assumed: `encode_video()` counts the packets in the output
and returns `None` on any mismatch, which keeps the frames and leaves the clip
without a `video` block rather than shipping a misaligned one.

⚠ **Read frame times from `poses.jsonl`, never from the video.** Sampling is not
uniform — the plugin pauses per frame, so real intervals range ~14–35 Hz even with
slow-mo off. The mp4 carries true presentation timestamps so it *plays* at correct
speed, but anything quantitative must use `game_time_ms`.

Verify a directory at any time:

```bash
tools/verify_alignment.py <out_dir>              # packet count vs pose count
tools/verify_alignment.py <out_dir> --content    # also decode and compare to the JPEGs
```

⚠ The `--content` check needs the JPEGs, so run it on a capture made with
`video_only: false`. It does **not** use a fixed error threshold: H.264-vs-JPEG
residual on an aligned pair is ~2.0 MAE, but the penalty for being off by one
depends entirely on scene speed — measured 3.34 at a slow moment and 13.07 at a
fast one in the same clip. Any fixed threshold either misses slow-scene
misalignment or fails good clips. It instead requires frame *k* to match jpg *k*
better than jpgs *k±1, k±2*, which is self-calibrating.

---

## 3. Install the plugin into GTA V

```
1. Copy ScriptHookV's bin/ files over DeepGTAV-PreSIL/bin/Release/
   (skip if you are on GTAV <= 1.0.3586.0)
2. Copy the contents of DeepGTAV-PreSIL/bin/Release/  ->  <GTAV install dir>
3. Replace Documents/Rockstar Games/GTA V/Profiles/  with the contents of
   DeepGTAV-PreSIL/bin/SaveGame
4. (Optional, recommended) install the mods under Mods/:
      Mods/Simple Increase Traffic(and Pedestrian)   <- more actors to interact with
      Mods/HeapLimitAdjuster                         <- fewer crashes with many spawns
      Mods/Graphics Settings
```

★ **`Simple Increase Traffic` is worth installing here even though it is optional
upstream.** Long-tail events need someone to have the event *with*; empty roads produce
boring clips at the exact locations you travelled furthest to reach.

---

## 4. Build the patched plugin

**Verified 2026-09-11**: builds clean with VS2022 Community (MSVC 14.35.32215, Windows SDK
10.0.22000.0), Release / x64, producing a 758,784-byte `DeepGTAV.asi`.

### 4a. Fetch dependencies (once)

```bash
tools/fetch_build_deps.sh
```

Three deps, all gitignored: **Eigen** (header-only), **cppzmq** (header-only), **libzmq**
(compiled straight into the `.asi`). ⬤ You do **not** need OpenCV, Boost, or
GTAVisionExport-DepthExtractor — this branch excludes `ObjectDet/ObjectDetection.cpp` and
`ObjectDet/LiDAR.cpp` behind `LONGTAIL_SLIM`, because it captures RGB + pose only. That is
the single biggest reason this build is tractable where upstream's is fiddly.

### 4b. Build

In VS2022: open `DeepGTAV-PreSIL/DeepGTAV.sln`, Release / x64.

Or from a shell (this is how it was verified — works from WSL via interop):

```bash
MSBUILD="/mnt/c/Program Files/Microsoft Visual Studio/2022/Community/MSBuild/Current/Bin/MSBuild.exe"
"$MSBUILD" 'D:\path\to\DeepGTAV-PreSIL\DeepGTAV.vcxproj' \
  -p:Configuration=Release -p:Platform=x64 -m -v:minimal
```

Result: `DeepGTAV-PreSIL/bin/Release/DeepGTAV.asi`. Copy it into the GTA V install dir.

**Sanity check the artifact** — its only DLL imports should be `WS2_32`, `IPHLPAPI`, `ADVAPI32`,
`KERNEL32`, `USER32`, `GDI32` and `ScriptHookV.dll`:

```bash
dumpbin /DEPENDENTS bin\Release\DeepGTAV.asi
```

⚠ If `libzmq-*.dll` or `opencv_world343.dll` appears there, you have built the upstream
configuration, not this one. libzmq is linked statically so nothing ships beside the `.asi`.

### 4c. Build gotchas, all of them real

These cost a build round each; they are recorded so they cost you none.

| Symptom | Cause |
|---|---|
| `unresolved external symbol Server::Server(unsigned int)` | ★★ **Object-file name collision.** libzmq has `src/server.cpp`; this project has `Server.cpp`. NTFS is case-insensitive, so both produce `Server.obj` in one `IntDir` and one silently clobbers the other. Fixed by `<ObjectFileName>$(IntDir)libzmq\</ObjectFileName>` on the libzmq item. **If you hit it after changing the project, delete `tmp/Release/*.obj`** — a stale clobbered `Server.obj` survives an incremental build. |
| `'s_camParams': undeclared identifier` in `Functions.h` | ⚠ `CamParams s_camParams` is *declared* in `CamParams.h` but **defined in `ObjectDetection.cpp`**, which this branch excludes. The definition lives in `LongtailStubs.cpp`; `Functions.h` was only ever getting `CamParams.h` transitively via `LiDAR.h`. |
| `C2589: '(' illegal token on right side of '::'` (100+ of them) | ⚠ **`NOMINMAX`.** OpenCV was defining it; without OpenCV, `windows.h`'s `min`/`max` macros shred every `std::min` call. Now in `PreprocessorDefinitions`. |
| `'vector'/'string'/'out_of_range' is not a member of 'std'` | Same class: those headers arrived transitively through `ObjectDetection.h` → OpenCV. Now declared explicitly. |
| `#error None of the ZMQ_IOTHREAD_POLLER_USE_* macros defined` | libzmq is normally configured by cmake. We compile it directly, so `builds/deprecated-msvc/platform.hpp` is written by `fetch_build_deps.sh`. |
| Errors in `ws_engine.cpp` / `SHA_DIGEST_LENGTH` undeclared | libzmq's WebSocket transport needs `ZMQ_HAVE_WS` + a SHA1 impl, and does not self-guard the way `pgm_`/`norm_`/`vmci_` do. Excluded via `Exclude="..\libzmq-4.3.5\src\ws*.cpp"` — we speak `tcp://` only. |

⚠ If you re-enable any ObjectDet feature (depth, stencil, segmentation, lidar), you must undo
the `LONGTAIL_SLIM` exclusions **and** restore the OpenCV / Boost / GTAVisionNative dependencies.
They are not independently toggleable.

---

## 5. Python environment

```bash
conda create -n DeepGTAV python=3.10 numpy ipykernel opencv matplotlib pyzmq
conda activate DeepGTAV
```

Verify the pose math before anything else:

```bash
cd VPilot
python longtail/test_posemath.py
```

Expected:

```
rotate() vs C++      : worst abs err 2.220e-16
basis orthonormality : worst abs err 4.441e-16
projection roundtrip : worst abs err 2.012e-11
non-square pixels    : OK
```

This test transcribes the plugin's C++ `rotate()` literally and compares it to the Python
implementation over 2000 random orientations. If it fails, stop — every pose you capture
will be wrong in a way that is invisible frame by frame.

---

## 6. Smoke test

1. Launch GTA V, wait for the game world to load.
2. From `VPilot/`:

```bash
python -m longtail.run_generator --out ./smoke --max-clips 3
```

You should see the ego teleport, drive, then something violent happen roughly a third of
the way into each clip, with output like:

```
[longtail] cut_in                 west_highway     frames=412  KEEP  (kept 1 / rej 0, 84 clips/h)
[longtail] ego_runs_red_light     ls_downtown      frames=286  DROP:1 pop-in event(s)  (kept 1 / rej 1, ...)
```

---

## 7. ★ Calibrate before you capture anything real

Do not skip this. A handedness error or a wrong FOV→K derivation gives you frames that
each look perfect and are mutually inconsistent — which a world model will absorb and no
amount of eyeballing single frames will reveal.

```bash
python -m longtail.calibrate ./smoke/clips/<clip_id>
```

Two checks:

**[1] Trajectory / forward-alignment** (no images, no opencv). A forward-mounted dashcam on
a car driving forwards must have its camera forward vector aligned with its direction of
travel. Interpretation:

| median angle | meaning |
|---|---|
| **< 25°** | OK |
| ~ 37–90° | axes swapped — check Euler order / basis assignment |
| ~ 180° | **forward vector inverted** — sign flip or handedness error |

**[2] Epipolar consistency** (needs opencv). Recovers the relative rotation between frame
pairs from image features using the logged `K`, and compares it against the relative
rotation from the logged poses. **Median rotation error should be < 3°.** This validates
intrinsics and pose convention together, end to end.

---

## 8. Run for real

```bash
python -m longtail.run_generator \
    --out F:/gtav_longtail \
    --d-min 140 \
    --seed 1
```

Runs until Ctrl-C. Resumes cleanly — the coverage ledger persists, so a restart continues
covering new ground rather than re-sampling the same places.

Tune via a JSON config:

```bash
python -c "from longtail.config import CaptureConfig; CaptureConfig().save('cfg.json')"
# edit cfg.json
python -m longtail.run_generator --config cfg.json --out F:/gtav_longtail
```

Config highlights:

| key | default | note |
|---|---|---|
| `clip_seconds_min/max` | 5 / 30 | clip duration is sampled per clip |
| `trigger_fraction_min/max` | 0.20 / 0.40 | event fires here, so you get before **and** after |
| `warmup_seconds_min` | 0.6 | floor only; the real gate is `WorldReady` (§0c) |
| `warmup_seconds_max` | 12.0 | hard cap so a bad spawn cannot stall the run |
| `require_world_ready` | true | ⚠ **do not disable** — without collision the car falls through the map |
| `require_scene_streamed` | false | costs warmup, buys fewer pop-in rejections |
| `pipeline_next_location` | true | pre-stream clip N+1 during clip N |
| `warmup_min_speed` | 3.0 | ego must be moving before the clip starts |
| `ttc_min` / `ttc_max` | 0.6 / 3.0 | ★ event difficulty axis; spawn geometry is solved from this |
| `ego_aggressiveness_min/max` | 0.4 / 1.0 | ⚠ upstream pinned this at 0.0 |
| `ego_ability_min/max` | 0.05 / 0.7 | ⚠ upstream pinned this at 100.0; **low** ability is the entropy source |
| `sample_driving_style_bits` | true | ~12 independent bits, not 5 presets |
| `density_*_min/max` | — | ambient traffic/ped/parked multipliers |
| `slowmo_probability` | 0.5 | ★ more frames through the impact |
| `slowmo_scale` | 0.25 | ⚠ costs proportional wall clock for that window |
| `outcome_bias` | true | chase rare **outcomes**, not rare setups |
| `outcome_bias_temperature` | 1.5 | >1 chases harder |
| `rate_hz` | 20 | ⚠ advisory only; real intervals are non-uniform, use `game_time_ms` |
| `popin_reject` | true | drops clips with photometric jumps unexplained by camera motion |
| `min_travelled_metres` | 8.0 | drops stuck spawns |

---

## 9. Output format

```
<out>/
  capture_config.json
  coverage_ledger.json          resume state; delete to start coverage over
  clips_index.jsonl             one line per clip: id, keep, scenario, region, frames
  clips/<clip_id>/
     frames/000000.png ...
     poses.jsonl
     meta.json
```

`poses.jsonl`, one object per frame:

```json
{"i": 137, "file": "frames/000137.png", "game_time_ms": 918342,
 "position": [1243.5, -812.9, 30.4], "theta_deg": [-1.9, 0.4, 172.6],
 "fov_deg": 50.0, "t": 6.85, "speed": 21.4}
```

`meta.json` carries `intrinsics` (fx, fy, cx, cy + the vertical FOV and reported aspect),
the scenario record (which event, its parameters, which frame it fired on), the
environment, the placement, and the quality verdict.

### Building a camera matrix

```python
from longtail import posemath
c2w = posemath.c2w_opencv(row["position"], row["theta_deg"])   # 4x4, OpenCV convention
c2w_gl = posemath.c2w_opengl(row["position"], row["theta_deg"]) # NeRF/OpenGL convention
K = posemath.intrinsics(meta["intrinsics"]["fov_deg_vertical"],
                        meta["intrinsics"]["width"], meta["intrinsics"]["height"],
                        meta["intrinsics"]["aspect_ratio_reported"])
```

**Conventions**, recorded in every `meta.json` so the dataset is self-describing:

- World: `+X` east, `+Y` north, `+Z` up, right-handed.
- `theta_deg` is `CAM::GET_CAM_ROT(cam, 0)` in degrees; `R = Rz(θz) · Ry(θy) · Rx(θx)`.
- `forward = R·[0,1,0]`, `up = R·[0,0,1]`, `right = R·[1,0,0]`.

⚠ **fx ≠ fy is possible and is not a bug.** The plugin derives horizontal extent from the
game's *reported* aspect ratio, which is not necessarily `width/height` — notably in DSR
modes where the captured buffer and the screen differ. Assuming square pixels there
silently shears every reconstruction. Always read `fx`/`fy` from `meta.json`.

---

## 9b. Outcome labelling — the part that makes "entropy" measurable

★ **Setup entropy is not outcome entropy.** Randomising sixty knobs still leaves
most clips as "the ego drove past and nothing happened", because whether an event
lands depends on execution, not configuration.

Every clip is labelled from the per-frame ego state into one of
`fire / rollover / major_collision / minor_contact / near_miss / no_event`, with
the raw measurements kept alongside so you can re-threshold without re-capturing:

```json
"outcome": {"label": "near_miss",
            "measurements": {"body_health_drop": 0.0, "peak_decel_mps2": 9.0,
                             "max_abs_roll_deg": 2.1, "collided": false,
                             "on_fire": false, "max_speed_mps": 24.3}}
```

⚠ **Deceleration alone does not imply a collision.** Emergency braking is ~9 m/s²
with no contact; labelling that a collision both mislabels the clip and starves
the near-miss class, which is the class real fleet logs are most short of and the
main reason for this generator. Severity is gated on damage; deceleration only
escalates a contact that already happened.

`OutcomeBiased` then reweights scenario selection by observed
`P(outcome | scenario)` scaled by inverse outcome share, so sampling chases the
gaps. Measured on a synthetic 3-scenario world (3000 draws):

| | `no_event` share | outcome entropy |
|---|---|---|
| bias off | 0.571 | 1.538 bits |
| bias on, T=1 | 0.492 | 1.663 bits |
| bias on, T=2 | 0.454 | 1.703 bits |

⚠ It cannot reach zero `no_event`: the floor is the best scenario's own miss
rate. Reweighting scenarios cannot beat the best scenario.

⚠ **A subtlety worth keeping**: an earlier scoring pass summed over *all* labels
with a Laplace prior, so labels nobody had produced had count 0 and their
`1/(1+0)` term dominated — identically for every scenario. That constant diluted
the signal and flattened the weight ratio from ~3.5 to ~1.25, i.e. the bias did
essentially nothing while appearing to work. The sum is now restricted to
outcomes actually observed; exploration is handled by `outcome_bias_min_history`.

Run-end summary reports outcome totals, entropy in bits, and per-scenario event
rate — so "maximise long-tail entropy" is something you can report against.

---

## 10. Scenario library

13 scenarios, weighted, filtered by whether the ego is moving fast enough to make the
event meaningful (`longtail/scenarios.py`).

**Ego-caused** — no spawned actors; the ego misbehaves and the world reacts:
`ego_runs_red_light`, `ego_into_cross_traffic`, `ego_swerve_into_oncoming`,
`ego_wrong_way`, `ego_loss_of_control` (forces wet/icy weather first),
`ego_emergency_brake`.

**Actor-caused** — spawned during warmup, triggered inside the clip:
`lead_vehicle_brake_check`, `cut_in`, `oncoming_head_on`, `side_impact_runner`,
`stalled_obstacle`, `emergency_scene`, `pedestrian_hazard`.

★ Half of these need no spawning at all. Running a red light is a **driving-style
bitmask**, not a scripted path — see `longtail/drivingstyles.py`, and run
`python longtail/drivingstyles.py` to decompose a preset before trusting it.

To add one, subclass `Scenario` with `setup(ctx)` / `trigger(ctx)` / `tick(ctx, t)` and
append it to `ALL_SCENARIOS`.

---

## 11. Coverage

`longtail/coverage.py` stratifies the map into 11 weighted regions and round-robins
between them with a `d_min` separation constraint.

⚠ **Two things this fixes.** First, the 2017 fork sampled `U[-2500, 2500]`, which misses
the entire north of the map — Paleto Bay, Mount Chiliad, the northern freeways, i.e.
exactly the long-range highway content. Second, uniform XY sampling over-samples empty
desert and ocean and under-samples the dense city where interactions need traffic to
interact with; hence per-region weights.

We cannot query the road graph from Python. We propose an `(x, y)` and the plugin snaps
it: `buildScenario()` calls `GET_CLOSEST_VEHICLE_NODE_WITH_HEADING`, so any proposal lands
on a drivable node. The ledger records the **snapped** position, which is what `d_min` is
enforced against. Region boxes are hand-drawn from the map — widen or split them freely.

---

## 12. Known limits and gotchas

- ⚠ **Frame intervals are not uniform.** `rate_hz` is a request. Use `game_time_ms`.
- ⚠ **ZeroMQ can reorder under load** (upstream's own README says so). The episode runner
  drops non-monotonic messages and records the count in `meta.json → timing.out_of_order_messages`.
  A rising count means you are pushing the queue too hard — lower resolution or rate.
- ⚠ **Nothing is reproducible.** GTA V's ambient traffic is not seeded in a way you
  control. Capture once and keep it; do not plan on re-rendering a clip.
- ⚠ **Ped scenarios are weak.** A true pedestrian dart-out needs `TASK_GO_STRAIGHT_TO_COORD`
  on a spawned ped wired through `Server.cpp`. `CreatePed`'s `task` field exists in the
  Python message and the C++ handler drops it. `pedestrian_hazard` places peds and lets
  ambient behaviour do the rest — weaker than the real thing.
- ⚠ **Reactivity is game-tuned, not physically calibrated.** Reaction times and braking
  profiles are arcade-y. Adequate as world-model training signal for *"what does a
  plausible near-miss look like"*; **not** a validated behaviour model, and it should not
  be described as one in any downstream write-up.
- ⚠ **Licence.** Every frame is derived from GTA V assets. That is a Rockstar EULA
  question, not an open-source one. Internal world-model training is a different risk
  posture from releasing a dataset — make it a conscious decision.

---

## 13. If you need CARLA instead

The trade is real and worth restating: CARLA gives exact intrinsics/extrinsics by
construction, deterministic replay, and a clean licence — but a handful of hand-built maps,
much lower visual variety, and traffic that is more scripted than reactive. GTA V gives a
large varied map and genuinely reactive ambient traffic, at the cost of earning pose
exactness through calibration and giving up reproducibility.

For **map-wide scene variation** and **reactive actors** — the two things this generator is
built around — GTA V is the right call. Step 7 is the price.
