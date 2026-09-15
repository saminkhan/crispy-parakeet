# Driving-clip capture — setup and run guide

This pipeline drives a car around GTA V, deliberately gets it into trouble, and
records short video clips with the exact camera pose for every frame. You give it
two paths and one settings file; it produces clips and keeps going on its own.

Each finished clip is a folder containing three files:

| file | what it is |
|---|---|
| `clip.mp4` | the video, played back at true speed |
| `poses.jsonl` | one line per frame: camera position, rotation, timestamp, speed |
| `meta.json` | what happened in the clip, and how it was captured |

★ **`clip.mp4` frame *k* is `poses.jsonl` line *k*.** That is the whole point of the
dataset, and the pipeline refuses to keep a clip where it isn't true.

---

## Before you start

You need **all** of these. The pipeline cannot work around a missing one.

1. **A Windows PC with a discrete GPU.** This has been run on an RTX 3080 Ti.
2. ⚠ **GTA V — the *Legacy* edition specifically**, legally purchased and installed.

   **This is the single easiest thing to get wrong.** Since March 2025 there are
   two different GTA V products, and buying "GTA V" on Steam today gives you the
   wrong one:

   | | supported here? | Steam app | installs to |
   |---|---|---|---|
   | **Grand Theft Auto V Legacy** | ✅ **yes** | `271590` | `...\common\Grand Theft Auto V` |
   | Grand Theft Auto V Enhanced | ❌ no | `3240220` | `...\common\Grand Theft Auto V Enhanced` |

   They are separate downloads with separate executables. The mod loader this
   pipeline depends on does not support Enhanced, and nothing will tell you that
   clearly — the game will simply start and the capture will never connect.

   If you own GTA V on Steam you already own Legacy; it is a separate entry in your
   library. In Steam, search your library for **"Grand Theft Auto V Legacy"** and
   install that one. If you only see "Enhanced", click the arrow next to the Play
   button — Legacy is usually listed there as a separate version.

   Note down the install path. It should end in `Grand Theft Auto V`, **not**
   `Grand Theft Auto V Enhanced`.
3. **WSL2 with Ubuntu**, and inside it: `python3`, `pip`, `ffmpeg`, `git`.
   ```bash
   sudo apt update && sudo apt install -y python3 python3-pip ffmpeg git
   pip3 install pyzmq numpy pillow
   ```
4. **About 100 GB of free disk** for 1000 clips. Clips average ~85 MB each.
5. **Visual Studio 2022**, with the "Desktop development with C++" workload —
   **only if you want to build the plugin yourself.** A prebuilt
   `DeepGTAV-PreSIL/bin/Release/DeepGTAV.asi` is included, so most people can skip
   this and Step 3 takes ten seconds.

⚠ **Do not run this on a machine you are also using.** The capture takes over the
game window and moves the mouse-free camera around. Let it have the machine.

---

## Step 1 — Confirm you have Legacy, not Enhanced

Launch GTA V once, normally, and let it reach the main menu. Then quit. This
creates the settings file a later step edits.

Now check you installed the right product. **Look at the folder name:**

```
...\steamapps\common\Grand Theft Auto V              correct — Legacy
...\steamapps\common\Grand Theft Auto V Enhanced     wrong — see the prerequisites
```

On Steam you can also confirm it from the library: right-click the game →
Properties → Updates, and the App ID should be **271590**.

This pipeline was built against **Legacy version 1.0.3889.0**. If your game
auto-updated past that, the mod loader may not have caught up yet; you will find
out in Step 5, and the fix is to wait for a matching release.

⚠ **Automatic launching assumes Steam.** If you own GTA V through the Rockstar
launcher or Epic instead, the pipeline can still drive it, but you must tell it how
to start the game — set `launch_command` in your settings file (Step 6) to that
launcher's URI or to the full path of `PlayGTAV.exe`.

---

## Step 2 — Install ScriptHookV

ScriptHookV is the mod loader the capture plugin runs inside.

1. Download it from <http://www.dev-c.com/gtav/scripthookv/>. Get the version that
   matches your game — the site lists which game version each build supports.
2. Open the downloaded zip. Inside `bin/` there are three files.
3. Copy **`ScriptHookV.dll`** and **`dinput8.dll`** into your GTA V folder — the same
   folder that contains `GTA5.exe`.

You do **not** need `NativeTrainer.asi`. Skip it.

---

## Step 3 — Install the capture plugin

**If `DeepGTAV-PreSIL/bin/Release/DeepGTAV.asi` already exists in this repo**, just
copy it into your GTA V folder next to `GTA5.exe`. Done — go to Step 4.

**If you need to build it:**

```bash
cd repos/DeepGTAV
bash tools/fetch_build_deps.sh          # downloads Eigen, cppzmq, libzmq
```

Then, in Windows, open `DeepGTAV-PreSIL/DeepGTAV.sln` in Visual Studio 2022, set the
configuration to **Release / x64**, and build. The result appears at
`DeepGTAV-PreSIL/bin/Release/DeepGTAV.asi`. Copy it into your GTA V folder.

⚠ If the build fails with *unresolved `Server::` symbols*, delete everything in
`DeepGTAV-PreSIL/tmp/Release/` and build again. Two source files in different
projects are both named `server.cpp`, and on Windows one silently overwrites the
other's compiled output. Clearing the folder fixes it.

---

## Step 4 — Tell Windows the game is DPI-aware

Skip this and your video will quietly come out at the wrong resolution.

```bash
cd repos/DeepGTAV
bash tools/setup_display.sh "D:\\SteamLibrary\\steamapps\\common\\Grand Theft Auto V"
```

Use your own GTA V path, with **double** backslashes as shown.

<details>
<summary>Why this matters</summary>

GTA V does not tell Windows it understands high-DPI displays. If your Windows
display scaling is above 100% — and on most laptops it is — Windows quietly gives
the game a smaller drawing surface than it asked for. On the machine this was built
on, the game *said* 1920×1080 and actually rendered **1708×960**.

Nothing reports this. The frames come out the right shape because the plugin
stretches them, so they look fine, but they are upscaled from a smaller image and
the camera information saved alongside them describes a picture that was never
drawn. This command sets the compatibility flag that fixes it. It changes one
per-user registry value and nothing else, and you can undo it from the exe's
Properties → Compatibility tab.
</details>

---

## Step 4b — Optional: remove chromatic aberration and lens distortion

Only needed if you plan to use the camera poses **geometrically** — reprojecting a
3D point to a pixel, depth, structure-from-motion. If you just want video, skip it.

GTA V bends the image slightly toward the corners (lens distortion) and splits the
colour channels there (chromatic aberration). `poses.jsonl` describes a plain
pinhole camera, which assumes neither. The error is zero at the centre and grows
to the edges, so it hides from any check done in the middle of the frame.

⚠ Unlike MSAA, depth of field and motion blur — which the pipeline turns off for
you — these two are not exposed in the game's settings. Removing them is a file
replacement using [OpenIV](https://openiv.com/), and always into the `mods` folder
rather than the real game files.

Full explanation and steps: see **[README.md](README.md#optional-remove-chromatic-aberration-and-lens-distortion)**.

---

## Step 5 — Find the address WSL uses to reach Windows

The capture script runs in WSL; the game runs in Windows. They talk over the
network, so WSL needs Windows' address.

```bash
ip route | grep default
```

You will see something like `default via 172.28.32.1 dev eth0`. **The number after
`via` is your address.** Write it down.

⚠ This address changes when you reboot. If a run suddenly cannot connect, check it
again.

---

## Step 6 — Write your settings file

Copy the example and open it in a text editor:

```bash
cd repos/DeepGTAV
cp capture.example.json capture.json
```

Find these three keys and change their values. ⚠ They are part of a larger file —
edit the values in place, do not replace the whole file with just this:

```json
  "gta_dir":    "D:/SteamLibrary/steamapps/common/Grand Theft Auto V",
  "output_dir": "D:/gtav_dataset",
  "host":       "172.28.32.1",
```

- `gta_dir` — where GTA V is installed. Use **forward** slashes.
- `output_dir` — where you want the clips saved. It will be created for you.
- `host` — the address from Step 5.

### The settings you will actually want to change

| setting | default | what it does |
|---|---|---|
| `target_clips` | `0` | How many clips to make. `0` means keep going until you stop it. |
| `clip_duration_s` | `15.0` | Length of each finished clip, in seconds. |
| `chaos` | `0.8` | How eventful the scenes are, from `0.0` to `1.0`. See below. |
| `ego_speed` | `null` | How fast your car is told to drive, in metres per second. `null` lets `chaos` decide. See below. |
| `graphics` | `"high"` | `low`, `medium`, `high`, or `ultra`. Lower is faster. |
| `width` / `height` | `1920` / `1080` | Video resolution. |
| `rate_hz` | `30` | Frames captured per second of in-game time. |
| `max_hours` | `0` | Stop after this many hours. `0` means no limit. |
| `keep_frames` | `false` | Keep the raw JPEGs as well as the mp4. Uses ~4× the disk. |
| `variations` | `[]` | Capture each scene several times with different behaviour. `"counterfactual"` gives the 2×2 grid described below, or list your own. `[]` is off. |
| `launch_command` | Steam Legacy | How to start the game. Only change this if you do **not** own GTA V on Steam. |

**`chaos` is the main dial.** At `0.0` you get quiet, lawful, sparse traffic. At
`1.0` you get dense traffic, aggressive drivers who do not brake for you, crowds of
pedestrians crossing in front of the car, and burning wrecks. It moves more than
thirty underlying settings together — traffic density, how many vehicles and people
get placed around you, how your car and everyone else's drives, and how close the
near-misses are set up to be. `0.8` is a good starting point.

⚠ **Higher chaos is not strictly better.** At `1.0` almost every clip ends in a
collision, so you get very few clips of ordinary driving or near-misses. If you want
a mix, run several batches at different `chaos` values into different `output_dir`s.

### Setting your car's speed

Leave `ego_speed` as `null` and `chaos` decides it: a calm run picks 10–18 m/s per
clip, a chaotic one 16–34. Set it yourself when you want speed to be a **fixed
quantity** rather than one more thing that varies — for example to capture the same
junction at 10, 20 and 30 m/s and compare, or to hold speed steady while behaviour
changes across variations.

Speeds are in **metres per second** (multiply by 3.6 for km/h, so 22 m/s ≈ 79 km/h).

```json
  "ego_speed": 22,
```

You can also give a range to sample from, `"ego_speed": [14, 30]`, and you can set
it from the command line without editing the file at all:

```bash
python3 run_capture.py --config capture.json --ego-speed 22
python3 run_capture.py --config capture.json --ego-speed 14-30
python3 run_capture.py --config capture.json --ego-speed 80kph
python3 run_capture.py --config capture.json --ego-speed auto     # back to chaos
```

Setting a speed does two extra things beyond picking the number. Your car is put at
that speed **at the moment recording starts**, rather than only when it spawns a few
seconds earlier — otherwise it spends those seconds braking for traffic and the clip
begins at roughly half the speed you asked for. And the staged incidents, which
normally command their own speed when they fire (a red-light run wants 22–34 m/s),
are held to your number instead, so the setting describes the whole clip rather than
just the start of it.

One exception: if your car has come to a stop before recording starts — blocked by a
vehicle, at a red light, already crashed — it is left stopped. Pushing it to speed
from a standstill would just ram it into whatever stopped it, and that collision
would be the tool's doing, not the driver's. `meta.json` records whether the speed
was asserted (`timing.speed_assert`) and how fast the car was going at that moment.

⚠ **It is a command, not a guarantee.** It sets the speed your car starts at and the
speed its driver aims for. After that the driving style, the traffic and the road
decide: a lawful car told to do 30 m/s still stops at red lights, and any car will
slow for the vehicle in front of it. Watch the `speed` field in `poses.jsonl` to see
what actually happened. Speeds above 60 m/s are refused — the game's driver cannot
hold a city road at those speeds and every clip becomes a crash into the first bend.

### Counterfactual variations

By default one place on the map gives you one clip. Set `variations` and the
pipeline captures the **same scene several times, changing only how people
behave**. The dataset then has overlap: the same intersection, the same staged
incident, but in one clip your car drives sensibly and in another it tries to hit
things.

```json
  "variations": "counterfactual",
```

**What a scene is.** Everything about one moment that has nothing to do with
behaviour: where you are, the weather, the time of day, the car you are driving,
how much traffic and how many people are placed around you, which incident is
staged, and where and when it is set up. With variations on, the pipeline picks a
scene once and then drives it N times.

**Held constant across the variations of one scene:**

- the location and its region
- weather, hour and minute
- your ego vehicle
- ambient traffic and pedestrian density
- how many vehicles, pedestrians and cyclists are placed around you, and where
- which incident is staged, and the random draws behind it — where the other car
  appears, when it moves. The scenario takes its random numbers from a seed
  derived from the scene, so it is set up the same way every time.

**What varies:**

- `ego_chaos` — your car. Driving style (obeys lights and lanes, or does not),
  aggressiveness, ability, speed.
- `actor_chaos` — everyone else. How other drivers drive and how fast, whether
  they steer around things or commit to contact, whether pedestrians cross in
  front of you, whether wrecks are set on fire, and how tight the staged near-miss
  is. ⚠ That last one means the incident is set up from the same draw but placed
  closer in a chaotic variation than in a calm one: same moment, same lane, less
  time to react.

`chaos` still sets the scene itself — the density and how many actors are placed.
`ego_chaos` and `actor_chaos` override only the behaviour parts, per variation.

⚠ **Keep `chaos` high when using variations.** At `chaos: 0.2` the street is nearly
empty, so `actors_chaotic` has almost nobody in it to be chaotic. `chaos` decides
how much is on stage; the variations decide how it behaves. `0.8`–`1.0` is right.

**The default four** (`"variations": "counterfactual"`):

| name | `ego_chaos` | `actor_chaos` | what it is for |
|---|---|---|---|
| `both_sane` | `0.0` | `0.0` | The baseline. Everybody behaves and the staged incident usually resolves as a near-miss or nothing. What the scene looks like driven properly. |
| `both_chaotic` | `1.0` | `1.0` | Everybody is reckless. The same as a plain `chaos: 1.0` clip — the worst case for this scene. |
| `ego_chaotic` | `1.0` | `0.0` | Only your car misbehaves. Isolates incidents your car causes: the same traffic, the same pedestrians, a driver who ploughs into them. |
| `actors_chaotic` | `0.0` | `1.0` | Only the world misbehaves. Isolates the "not your fault" case: what a careful driver sees when others cut in, run lights and step out. |

Together they let you ask, of the same moment: did the collision happen because of
the ego, because of the others, or only when both were at fault?

**Writing your own list.** Any number of variations, each with a name and two
dials:

```json
  "variations": [
    {"name": "baseline",     "ego_chaos": 0.0, "actor_chaos": 0.0},
    {"name": "mild_traffic", "ego_chaos": 0.0, "actor_chaos": 0.5},
    {"name": "wild_traffic", "ego_chaos": 0.0, "actor_chaos": 1.0}
  ]
```

Names become part of folder names, so keep them short — letters, digits and
underscores. The dials run `0.0`–`1.0` like `chaos`. Behaviours that are on or off
(do pedestrians cross, do wrecks burn, do drivers steer around things) switch at
`0.5`; everything else slides smoothly between calm and chaotic. `[]` turns
variations off and the pipeline behaves exactly as it did before.

★ **The population is reproduced too.** With `deterministic_population` on —
the default whenever variations are in use — the game's own background traffic is
cleared before each clip and its spawner held at zero, and every person and car in
the scene is placed *by the pipeline* from the scene's seed. Same scene, same
people, same cars, same places. GTA offers no way to seed its own spawner, so this
is the only route to a reproducible scene; the cost is that background traffic
beyond what is seeded is gone. Raise the `seed_*` counts if a scene feels empty.

⚠ **What is still *not* reproduced: physics and AI.** Two variations of a scene
are **not frame-identical replays**. The same car placed in the same spot with the
same driver will not follow a bit-identical trajectory across runs — the engine's
timing is not exact. What you get is the same *situation*, not the same pixels.
That is enough to compare outcomes; it is not enough to diff frames.

Set `"deterministic_population": false` to keep the game's ambient traffic in
variation runs, or `true` to have reproducible populations in single-clip runs.

**What it looks like on disk.** Clip folders are named `<scene_id>-<variation>`,
so the variations of one scene sort together:

```
clips/3f9a2c17d5e8-both_sane/
clips/3f9a2c17d5e8-both_chaotic/
clips/3f9a2c17d5e8-ego_chaotic/
clips/3f9a2c17d5e8-actors_chaotic/
```

Each `meta.json` carries a `scene` block — the scene id, its seed, and which
variation this clip is — and its `environment` block records the `ego_chaos` and
`actor_chaos` used. Lines in `manifest.jsonl` carry `scene_id` and `variation`
too. Every clip still passes its own quality checks, so a scene can end up with
three kept clips and one rejected; the manifest is where you see that.

**Checking the effect.** `scenes.jsonl` in the output folder has one line per
scene: its id, location, conditions, the staged scenario, and how each variation
turned out. `summary.json` gains a per-variation table — outcomes broken down by
variation name — so one glance tells you whether `both_sane` really produces fewer
collisions than `ego_chaotic`. If it does not, the dials are not doing what you
think, and that is worth knowing before you capture a thousand more.

A few more things to know:

- `clips_per_lifetime` is rounded **up** to a multiple of the variation count, so
  a game lifetime never ends in the middle of a scene. With the default four, `12`
  stays `12` and `10` becomes `12`.
- `target_clips` still counts clips, not scenes. `400` with the default four
  variations is 100 scenes.
- ⚠ If the game crashes in the middle of a scene, that scene is marked
  **incomplete** in `scenes.jsonl`. The variations that finished are kept as
  normal clips. Filter on that flag if your analysis needs whole scenes.

---

## Step 7 — Test your settings without starting the game

```bash
python3 run_capture.py --config capture.json --dry-run
```

This checks everything and prints what it *would* do. It does not launch the game.

If it prints problems, fix them and run it again. Common ones:

| message | fix |
|---|---|
| `gta_dir does not exist` | Check the path, and use forward slashes. |
| `no GTA5.exe in gta_dir` | You pointed at the wrong folder. You want the one containing `GTA5.exe`. |
| `output_dir is not writable` | Pick a folder on a drive you can write to. |
| `chaos must be between 0.0 and 1.0` | You typed a number outside the range. |

---

## Step 8 — Start capturing

```bash
python3 run_capture.py --config capture.json
```

Now leave it alone. It will:

1. Write your graphics settings into the game's config.
2. Launch GTA V and wait for it to be ready (about 90 seconds).
3. Start driving, staging incidents, and recording clips.
4. Finish and tidy each clip **in the background** while it captures the next one.
5. Delete clips that failed their quality checks, keeping only a record of why.
6. Relaunch the game if it crashes, and carry on.

Expect roughly **25–40 finished clips per hour** — measured on an RTX 3080 Ti, so
treat it as a rough guide rather than a promise. Slower hardware, a slower disk, or
a higher `graphics` preset all reduce it.

⚠ **The game window will look terrible while this runs** — jerky, stuttering,
freezing between frames. That is normal and it is not in your clips. The capture
pauses the game to take each frame; the recorded video is smooth. This was measured,
not guessed.

⚠ **The game will crash sometimes** — measured at roughly one game lifetime in
three over an overnight run, for reasons that are not understood. The script notices, restarts it, and continues.
You do not need to do anything.

⚠ **If your machine has a shared-GPU lock script** (`tools/gpu_lock.sh` in the
parent repo of this one), do **not** wrap `run_capture.py` in `gpu_acquire`
yourself. The runner finds that script and takes the lock around each capture
chunk on its own. Wrapping it again from outside makes the inner acquire wait
forever for a lock its own parent holds — the run sits at "ensuring game is up"
producing nothing. On a machine without that script the runner simply skips it.

### Watching progress

In another terminal:

```bash
python3 run_capture.py --config capture.json --status
```

### Stopping

Press **Ctrl-C** once, and wait. It will finish tidying up and write a final
summary. Pressing Ctrl-C repeatedly can leave a clip half-written.

### Resuming

Run the exact same command again:

```bash
python3 run_capture.py --config capture.json
```

It reads what is already in `output_dir`, counts it, and carries on toward your
target. It will not redo work or overwrite anything.

---

## Step 9 — What you get

```
D:/gtav_dataset/
├── clips/
│   ├── 0959fed6d82c1a4b/                one clip, when variations are off
│   │   ├── clip.mp4
│   │   ├── poses.jsonl
│   │   └── meta.json
│   ├── 3f9a2c17d5e8-both_sane/          one scene = four folders, when variations are on
│   │   ├── clip.mp4
│   │   ├── poses.jsonl
│   │   └── meta.json
│   ├── 3f9a2c17d5e8-both_chaotic/
│   ├── 3f9a2c17d5e8-ego_chaotic/
│   ├── 3f9a2c17d5e8-actors_chaotic/
│   └── ...
├── manifest.jsonl        every clip ever made, one line each, including rejected ones
├── scenes.jsonl          one line per scene, only when variations are on
└── summary.json          totals, kept vs rejected, breakdown by outcome, region and variation
```

`manifest.jsonl` is the living record. It is only ever appended to, so it survives
the script being killed, and it is what lets a restart pick up where it left off.

### Reading `meta.json`

The parts you are most likely to want:

- `outcome.label` — what happened: `major_collision`, `minor_contact`, `near_miss`,
  `rollover`, `fire`, or `no_event`.
- `outcome.measurements` — numbers behind that label: peak deceleration, damage to
  the car, how many other vehicles were damaged or set on fire.
- `scenario` — which incident was staged, and at which frame it was triggered.
- `environment` — weather, time of day, which car you were driving, and the
  `ego_chaos` / `actor_chaos` this clip was captured with.
- `scene` — which scene this clip belongs to and which variation it is. `null`
  when `variations` is off.
- `capture` — the resolution, frame rate and camera setup used.

### Reading `poses.jsonl`

One JSON object per line, one line per video frame:

```json
{"i": 0, "game_time_ms": 337011, "position": [-560.4, -1187.4, 20.6],
 "theta_deg": [0.1, 0.0, 143.2], "fov_deg": 50.0, "t": 0.0, "speed": 19.7}
```

⚠ **Use `game_time_ms` for timing, never `1 / rate_hz`.** Frames are not evenly
spaced — the real interval varies between about 14 and 35 per second. The mp4 stores
the true timing so it *plays* correctly, but any calculation you do must use the
timestamps.

---

## If something goes wrong

**Nothing happens for several minutes after starting.**
Normal. Launching GTA V and loading the world takes about 90 seconds, and the first
clip needs another minute.

**`Connection refused` or `Resource temporarily unavailable`.**
The script cannot reach the game. Check the `host` address (Step 5) — it changes on
reboot. If the address is right, the plugin is probably not loading: look at
`ScriptHookV.log` in your GTA V folder.

**The game starts but the script never connects, and `ScriptHookV.log` does not
exist at all.**
You are almost certainly running **Enhanced** rather than **Legacy**. Check the
install folder name — if it ends in `Grand Theft Auto V Enhanced`, that is the
problem. Install "Grand Theft Auto V Legacy" (Steam app 271590) and point `gta_dir`
at that folder instead. See the prerequisites.

**`ScriptHookV.log` says the game version is not supported.**
Your game updated. Get the matching ScriptHookV from Step 2. If none exists yet, you
must wait for one to be released.

**Clips are being made but nearly all are rejected.**
Look at `summary.json` for the reasons. The usual causes are a badly chosen
`output_dir` on a slow drive, or `chaos` set so high that the car is destroyed before
the clip can finish.

**The video is not 1920×1080 even though I asked for it.**
You skipped Step 4, or it did not take. Check `DeepGTAV.log` in the GTA V folder:

```
[longtail] backbuffer 1920x1080 ... (target 1920x1080)   correct
[longtail] backbuffer 1708x960  ... (target 1920x1080)   wrong — redo Step 4
```

**I want my original graphics settings back.**
The pipeline backed them up the first time it ran. In your Windows Documents folder,
under `Rockstar Games/GTA V/`, replace `settings.xml` with `settings.xml.orig`.

**Checking that a clip's video and poses really line up:**

```bash
python3 tools/verify_alignment.py D:/gtav_dataset
```
