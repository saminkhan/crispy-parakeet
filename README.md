# Long-tail driving-clip capture

Turns GTA V into a generator of short ego-perspective driving clips with a
per-frame camera pose, aimed at the safety-critical events that are rare in real
fleet logs: collisions, near-misses, rollovers, vehicle fires, and pedestrians
stepping into the road.

Each kept clip is a folder with three files:

| file | contents |
|---|---|
| `clip.mp4` | the video, played back at true speed |
| `poses.jsonl` | one line per frame — camera position, rotation, FOV, timestamp, speed |
| `meta.json` | what happened in the clip, and how it was captured |

★ **`clip.mp4` frame *k* is `poses.jsonl` line *k*.** The pipeline refuses to keep a
clip where that is not true.

## Quick start

```bash
cp capture.example.json capture.json     # set gta_dir and output_dir
python3 run_capture.py --config capture.json --dry-run
python3 run_capture.py --config capture.json
```

⚠ **You need GTA V *Legacy*, not *Enhanced*.** They are two different products;
Enhanced is not supported. **[SETUP.md](SETUP.md) is the full step-by-step guide** —
read it before running anything, as several of the setup steps fail *silently* if
skipped.

## What it does

Runs unattended. It launches the game, drives, stages incidents, records clips,
finalises each one in the background while the next is already capturing, throws
away the ones that fail their quality checks, and restarts the game when it
crashes. Stop it with Ctrl-C; run the same command again to carry on where it left
off.

Roughly 25–40 finished clips per hour, about 85 MB each.

**Counterfactual variations.** Set `variations` and each scene is captured several
times with different behaviour: the same intersection, weather, hour, ego vehicle
and staged incident, but in one clip the ego drives sanely and in another it tries
to hit things — with the other road users sane or reckless independently. The
default is a 2×2 grid (`both_sane`, `both_chaotic`, `ego_chaotic`,
`actors_chaotic`); any list of named `ego_chaos` / `actor_chaos` pairs works. With
`deterministic_population` on (the default for variations) the game's background
traffic is cleared and every actor is seeded from the scene, so the same scene gets
the same people and cars. ⚠ Physics and AI are still not bit-exact across runs:
same situation, not the same pixels. What is and is not held constant is spelled
out in [SETUP.md](SETUP.md#counterfactual-variations).

## Configuration

Nineteen keys in one JSON file — resolution, frame rate, clip length, graphics
preset, how many clips you want, and a single `chaos` dial from 0.0 to 1.0 that
moves some thirty underlying knobs together, from calm lawful traffic to dense
aggressive traffic with crowds crossing in front of the car. An optional
`variations` list splits that dial into a per-pass `ego_chaos` and `actor_chaos`,
so one scene can be captured with the ego and the other road users each sane or
reckless while everything else about the scene is held fixed. See
[SETUP.md](SETUP.md#step-6--write-your-settings-file).

Individual knobs can also be lifted out of the `chaos` dial and commanded directly.
`ego_speed` (or `--ego-speed 22`, `--ego-speed 14-30`) fixes how fast the ego is
told to drive, holds the staged incidents to that number instead of their own, and
asserts it at the frame recording starts — so speed becomes a control variable held
across a scene's variations rather than one more thing chaos moved. ⚠ It commands
the entry speed and the driver's target; the driving style, the traffic and the road
still own what happens afterwards.

## Layout

```
capture/          the pipeline: settings, game lifecycle, background finalizer,
                  manifest, supervised runner
run_capture.py    entry point
VPilot/longtail/  capture client: region sampling, staged scenarios, outcome
                  labelling, pose maths, quality gates
DeepGTAV-PreSIL/  the ScriptHookV plugin (C++); prebuilt DeepGTAV.asi included
tools/            game lifecycle helpers, display setup, verification
SETUP.md          step-by-step setup and run guide
```

## Optional: remove chromatic aberration and lens distortion

<https://www.gta5-mods.com/misc/no-chromatic-aberration-lens-distortion-1-41>

**Recommended if you intend to use the camera poses geometrically** — for
reprojection, depth, structure-from-motion, or anything that maps a 3D point to a
pixel. Skip it and the clips still look fine; the poses just stop being exactly
true near the frame edges.

### Why

`poses.jsonl` describes a **pinhole camera**: a position, a rotation, and a field
of view. That model says a 3D point projects to one pixel by a straight-line
projection, with no bending.

GTA V applies two post-process effects that break that promise, *after* the scene
has been projected:

- **Lens distortion** bends the image radially, so a straight edge in the world
  lands on a curved line of pixels.
- **Chromatic aberration** splits the colour channels radially, so red, green and
  blue for the same surface land on slightly different pixels.

Both are zero at the centre of the image and grow toward the corners. So a
reprojection check done in the middle of the frame agrees beautifully and hides the
problem, while the same check at the edges is quietly wrong — the failure mode is
one that validates cleanly and misleads you later.

⚠ This is the same reason `capture/settings.py` forces **MSAA off**, **depth of
field off** and **motion blur to zero** at every graphics preset. Those three are
reachable from the game's own settings; chromatic aberration and lens distortion
are not, which is the only reason this one is a manual step rather than something
the pipeline sets for you.

★ Upstream installed this mod too, for a different reason worth knowing: with it,
2D bounding boxes at the edges of the frame finally lined up with the objects. That
is independent evidence of the same underlying distortion, observed from the
labelling side rather than the geometry side — and it is also proof that turning
`PostFX` down is not sufficient on its own.

### How

The effects live in the game's compiled shader files, so this is a **file
replacement**, not a setting. You need [OpenIV](https://openiv.com/).

1. Install OpenIV and let it set up **ASI Manager → openIV.asi** and the **`mods`
   folder** for your GTA V install.
2. ⚠ **Always edit inside the `mods` folder, never the real game files.** OpenIV
   offers to copy the archive you are editing into `mods/` first — accept. This
   keeps the original untouched, so a game update or a Rockstar integrity check
   does not find modified originals, and you can undo everything by deleting one
   folder.
3. Download the mod from the link above and follow the install instructions on that
   page — it tells you which archive and path to replace. Those paths differ
   between mod versions, so use its instructions rather than any copied here.
4. Start the game once and check a frame near the edge: straight world edges
   (lamp posts, building corners, lane markings) should be straight all the way
   into the corners, with no colour fringing.

To undo it, delete the `mods` folder. Nothing in the real game files changed.

⚠ Only relevant to GTA V **Legacy**, like everything else here.

## Credits and licence

This repository is built on a chain of earlier work. **If you use it, please cite
those works as well** — the request is upstream's and it is a fair one.

- **DeepGTAV** — the original framework: <https://github.com/aitorzip/DeepGTAV>
- **GTAVisionExport** — added segmentation export, from *"Driving in the Matrix: Can
  Virtual Worlds Replace Human-Generated Annotations for Real World Tasks?"*
  (<https://arxiv.org/abs/1610.01983>): <https://github.com/umautobots/GTAVisionExport>
- **DeepGTAV-PreSIL** — combined the two, from *"Precise Synthetic Image and LiDAR
  (PreSIL) Dataset for Autonomous Vehicle Perception"*
  (<https://arxiv.org/abs/1905.00160>): <https://github.com/bradenhurl/DeepGTAV-PreSIL>
- **David0tt/DeepGTAV** — the maintained fork this one starts from, which added UAV
  capture and brought the toolchain up to date:
  <https://github.com/David0tt/DeepGTAV> (<https://arxiv.org/abs/2112.12252>)

This fork replaces the UAV/object-detection path with ego-perspective long-tail
clip capture: staged incidents, per-frame camera pose, outcome labelling, and an
unattended runner. Licensed GPL-3.0 as inherited — see [LICENSE](LICENSE).

## Use considerations

⚠ **Rockstar permits non-commercial use of GTA V footage**, which covers academic
work; commercial use of game footage is generally not allowed. See Rockstar's
[Policy on posting copyrighted Rockstar Games material](https://support.rockstargames.com/articles/7bNaeoMFTV0iUDGhStTXvz/policy-on-posting-copyrighted-rockstar-games-material). Everything this pipeline produces is derived from
GTA V assets, so *redistributing* the output is a Rockstar EULA question.

⚠ **You must supply GTA V and ScriptHookV yourself.** Neither is included, and a
game patch could introduce breaking changes at any time.
