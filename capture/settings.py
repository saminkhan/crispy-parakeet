"""User-facing capture settings.

One small JSON file is the whole product surface: paths, image shape, how long a
clip is, how eventful it should be and, optionally, which counterfactual
variations of every scene to capture. Everything else -- the 90 fields of
`longtail.config.CaptureConfig`, the GTA V settings.xml -- is derived from it
here, so a user never has to learn either.

    from capture.settings import CaptureSettings
    s = CaptureSettings.load("capture.json")
    problems = s.validate()
    cfg = CaptureConfig(**s.to_generator_config())      # the single-dial config
    for variation, gen_cfg in s.variation_configs():    # [] unless variations is set
        ...
"""

import difflib
import json
import os
import re
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Chaos scaling
# ---------------------------------------------------------------------------
# `chaos` is one 0..1 dial over the knobs that actually decide whether a clip
# contains an event. Every knob interpolates between a calm and a chaos value.
# The knobs are partitioned into three groups by WHAT they describe:
#
#   SCENE  -- who is there: ambient density multipliers and seeded population
#             counts. Part of the scene's identity.
#   EGO    -- how the ego drives: aggressiveness, ability, restraint bits, speed.
#   ACTOR  -- how everyone else behaves: NPC driving style and skill, whether
#             traffic is re-tasked at the ego, whether peds step into the road,
#             the TTC the staged event is solved for, and wreck ignition.
#
# ★ The partition exists for counterfactual capture (`variations`, below): the
#   same scene is captured N times and only EGO and ACTOR may differ between the
#   captures, each on its own dial. SCENE knobs always come from the top-level
#   `chaos`, so density and population counts -- and, through a scene-derived
#   seed, the staged incident's geometry -- are identical across a scene's
#   variations. A knob filed in the wrong group is a knob that silently varies
#   between clips that are supposed to differ only in behaviour. File new knobs
#   deliberately.
#
# ★ The chaos=1.0 column is COPIED, value for value, from
#   VPilot/longtail/configs/max_chaos_15s.json -- the config the existing dataset
#   was captured with -- and, for the NPC behaviour knobs that config predates,
#   from the CaptureConfig defaults in longtail/config.py, which ARE the
#   max-chaos values the plugin shipped hard-coded. Every number was arrived at
#   by measurement, and the reasoning is on the corresponding field in
#   longtail/config.py (why 850 and not 400 for ignition, why density multipliers
#   cannot substitute for seeded actors, why TTC is sampled and geometry solved
#   backwards). Do not "improve" these by intuition. Change the tuned config,
#   capture against it, measure, and only then copy the new number here.
#
# ⚠ The chaos=0.0 column is a design choice, not a measurement: nobody has run a
#   calm batch end to end. It is aimed at "quiet residential street, driver who
#   passed his test, traffic that obeys the rules" -- sparse population, high
#   ability and near-zero aggression for ego and NPCs alike, restraint bits
#   almost always set, stock traffic AI with avoidance on, peds that stay on the
#   pavement, TTCs long enough that the staged event resolves without contact,
#   and no forced ignitions.
#
# ⚠ Interpolation is linear and per-knob for numbers, which keeps every min<=max
#   pair ordered at every chaos value (both endpoints are ordered, so all convex
#   combinations are). Booleans and bitmasks have no midpoint and SWITCH at 0.5:
#   the calm value below it, the chaos value at or above it. Neither means
#   chaos=0.5 yields half the event rate: event yield is very non-linear in
#   density -- measured, 0.7 vehicles damaged per clip with <=4 vehicles within
#   60 m vs 3.3 with >=8.
#
# Each table maps a CaptureConfig field to (calm, chaos): the chaos=0.0 and the
# chaos=1.0 value side by side, so a knob cannot be added to one column and
# forgotten in the other.

#: SCENE -- always scaled by the top-level `chaos`; constant across the
#: variations of one scene.
_SCENE = {
    # Ambient population multipliers. These SCALE GTA's own population model, so
    # below 1.0 is genuinely emptier than stock.
    "density_vehicle_min": (0.3, 2.0),
    "density_vehicle_max": (0.8, 3.0),
    "density_ped_min": (0.2, 3.0),
    "density_ped_max": (0.6, 5.0),
    "density_parked_min": (0.2, 1.0),
    "density_parked_max": (0.6, 2.0),
    # Explicitly placed actors. Near zero at the calm end: the multipliers alone
    # leave a quiet street quiet, which is the point at that end of the dial.
    "seed_vehicles_min": (0, 6),
    "seed_vehicles_max": (3, 18),
    "seed_peds_min": (0, 6),
    "seed_peds_max": (3, 20),
    "seed_cyclists_min": (0, 2),
    "seed_cyclists_max": (1, 8),
}

#: EGO -- scaled by ego_chaos (the top-level `chaos` unless a variation says
#: otherwise).
_EGO = {
    # Aggressiveness ~0 and ability ~1 is upstream's "never takes a risk, never
    # makes a mistake" driver.
    "ego_aggressiveness_min": (0.0, 0.95),
    "ego_aggressiveness_max": (0.1, 1.0),
    "ego_ability_min": (0.9, 0.0),
    "ego_ability_max": (1.0, 0.1),
    # P(restraint bit set) in drivingstyles.sample -- higher is MORE lawful.
    "driving_style_bit_p": (0.95, 0.03),
    "driving_style_license_p": (0.0, 0.25),
    # Ego speed envelope. 34 m/s on an empty suburban road is not calm whatever
    # the driving style says.
    "ego_cruise_speed": (14.0, 22.0),
    "ego_speed_min": (10.0, 16.0),
    "ego_speed_max": (18.0, 34.0),
}

#: ACTOR -- scaled by actor_chaos (the top-level `chaos` unless a variation says
#: otherwise).
_ACTOR = {
    # Time-to-collision the event geometry is solved for. Long enough at the calm
    # end that a competent driver resolves it without contact.
    "ttc_min": (3.0, 0.3),
    "ttc_max": (6.0, 0.9),
    # Body health below which a wreck is ignited. Measured: the worst-hit vehicle
    # across a seeded set reached 766 from a starting 1000, so anything under
    # that never fires -- 0.0 means "only an already-destroyed vehicle".
    "ignite_below_health": (0.0, 850.0),
    "ignite_wrecks": (False, True),
    # How every non-ego road user drives. Calm is DRIVINGMODE_NORMAL (786603:
    # stop for cars and peds, obey lights, avoid everything); chaos is
    # ALLOW_WRONG_WAY | TAKE_SHORTEST_PATH. A bitmask, so it switches rather
    # than interpolates -- see _SWITCH_KNOBS.
    "npc_driving_style": (786603, 512 | 262144),
    "npc_aggressiveness": (0.0, 1.0),
    # ⚠ LOWER ability is WORSE driving, so this pair runs 1 -> 0.
    "npc_ability": (1.0, 0.0),
    "npc_cruise_speed": (20.0, 40.0),         # m/s
    "npc_steers_around": (True, False),       # True = stock avoidance
    # Master switches for the plugin's re-task sweep (traffic aimed at the ego)
    # and for peds stepping into the road. Off at the calm end: sane traffic is
    # not traffic that has merely been asked to be polite about hitting you.
    "traffic_aggression": (False, True),
    "ped_interaction": (False, True),
    "ped_cross_chance": (0.0, 0.35),
}

#: ⚠ episode.py feeds these to rng.randint, which raises on a float. Interpolated
#: values must be rounded back to int before they reach CaptureConfig.
_INT_KNOBS = frozenset((
    "seed_vehicles_min", "seed_vehicles_max",
    "seed_peds_min", "seed_peds_max",
    "seed_cyclists_min", "seed_cyclists_max",
))

#: Categorical ints. Half of ALLOW_WRONG_WAY is a different flag, not a milder
#: one, so a driving-style bitmask switches at 0.5 like the booleans do (those
#: are recognised by type and need no listing here).
_SWITCH_KNOBS = frozenset(("npc_driving_style",))

# Checked at import because each of these would fail silently at run time: a knob
# filed in two groups is scaled twice from two dials and the last write wins; a
# knob named in _INT_KNOBS or _SWITCH_KNOBS but absent from every table quietly
# becomes a linearly interpolated float; a pair mixing a bool with a number
# interpolates True as 1.
_GROUPS = (_SCENE, _EGO, _ACTOR)
_ALL_KNOBS = set()  # type: set
for _table in _GROUPS:
    _dup = _ALL_KNOBS.intersection(_table)
    if _dup:
        raise RuntimeError("knob filed in two chaos groups: %s" % ", ".join(sorted(_dup)))
    _ALL_KNOBS.update(_table)
    for _name, (_calm, _hot) in _table.items():
        if isinstance(_calm, bool) != isinstance(_hot, bool):
            raise RuntimeError("chaos pair for %s mixes bool and number" % _name)
_stray = (_INT_KNOBS | _SWITCH_KNOBS) - _ALL_KNOBS
if _stray:
    raise RuntimeError("knob kind listed for unknown knob(s): %s" % ", ".join(sorted(_stray)))
del _table, _dup, _name, _calm, _hot, _stray


def _scale_knobs(table, chaos):
    """Interpolate one group's (calm, chaos) pairs at `chaos` in 0..1.

    Returns a dict of CaptureConfig fields.
    """
    c = min(1.0, max(0.0, float(chaos)))
    out = {}
    for name, (calm, hot) in table.items():
        if isinstance(calm, bool) or name in _SWITCH_KNOBS:
            out[name] = hot if c >= 0.5 else calm
            continue
        v = calm + (hot - calm) * c
        if name in _INT_KNOBS:
            out[name] = int(round(v))
        else:
            # Round for the sake of the capture_config.json the generator writes
            # next to the dataset: 0.30000000000000004 in a provenance record is
            # noise, and nothing downstream needs the last four ulps.
            out[name] = round(v, 4)
    return out


# ---------------------------------------------------------------------------
# Counterfactual variations
# ---------------------------------------------------------------------------
# One proposed location normally yields one clip. With `variations` set, the
# generator captures the SAME scene once per variation: same (x, y), region,
# weather, clock, ego vehicle model, density multipliers, seeded population
# counts, the same staged scenario and -- through a seed derived from the scene
# -- the same spawn geometry, timing and TTC draw for it. Only the EGO and ACTOR
# knob groups differ, each scaled by the variation's own dial. So a dataset can
# hold the same intersection and the same staged incident with the ego driving
# sanely in one clip and trying to hit things in the next.
#
# ⚠ What is reproduced is location, conditions, ego vehicle, the staged incident
#   and seeded-actor placement. GTA's ambient traffic and its choice of random
#   ped and vehicle models are the game's own randomness, and frame-identical
#   replay is NOT possible. Variations of a scene overlap; they are not the same
#   footage with one thing changed.
#
# A variation is a plain dict {"name": str, "ego_chaos": 0..1, "actor_chaos":
# 0..1}. `variations` in capture.json is either a list of them or the name of a
# preset below. [] -- the field default -- turns the feature off.
#
# ⚠ Off is NOT byte-identical to the pre-variations behaviour below chaos=1.0, and
# that is deliberate. The ACTOR knobs used to be hard-coded at their max-chaos
# values inside the plugin regardless of the dial, so "chaos 0.8" was a scene with
# calmer density and an ego at 0.8 -- surrounded by traffic still at 1.0. Now the
# dial scales them too (npc_aggressiveness 1.0 -> 0.8, npc_ability 0.0 -> 0.2,
# ped_cross_chance 0.35 -> 0.28 at 0.8; the on/off knobs flip below 0.5). At
# chaos=1.0 nothing changes. The dial finally means what it says.

#: Preset variation lists. load() expands the name into the list.
PRESETS = {
    # The 2x2 grid over "who misbehaves": four clips per scene.
    "counterfactual": [
        {"name": "both_sane",      "ego_chaos": 0.0, "actor_chaos": 0.0},
        {"name": "both_chaotic",   "ego_chaos": 1.0, "actor_chaos": 1.0},
        {"name": "ego_chaotic",    "ego_chaos": 1.0, "actor_chaos": 0.0},
        {"name": "actors_chaotic", "ego_chaos": 0.0, "actor_chaos": 1.0},
    ],
}

_VARIATION_KEYS = ("name", "ego_chaos", "actor_chaos")
#: The name becomes part of a clip directory name ("<scene_id>-<name>"), so
#: nothing a filesystem or a shell would argue with. ⚠ Matched with fullmatch(),
#: not match() against ^...$: "$" accepts a trailing newline, and "sane\n" would
#: pass validation and then produce a directory nobody can type.
_VARIATION_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


def _variation_problems(variations):
    """Human-readable problems with a `variations` value; [] means OK.

    A str is a preset name. A list is checked entry by entry: every entry needs
    the three keys and nothing else, a unique directory-safe name, and both dials
    in 0..1.
    """
    shape = "a list of {name, ego_chaos, actor_chaos} objects"
    if isinstance(variations, str):
        if variations in PRESETS:
            return []
        return ["variations %r is not a known preset; use one of: %s -- or give %s"
                % (variations, ", ".join(sorted(PRESETS)), shape)]
    if not isinstance(variations, list):
        return ["variations must be a preset name or %s ([] = off), got %r"
                % (shape, variations)]

    p = []  # type: List[str]
    seen = {}  # type: Dict[str, int]
    for i, v in enumerate(variations):
        where = "variations[%d]" % i
        if not isinstance(v, dict):
            p.append("%s must be an object with name, ego_chaos and actor_chaos, got %r"
                     % (where, v))
            continue
        # Same rule as load(): a typo'd key that is silently ignored is a dataset
        # captured with a dial the user believes they set.
        missing = [k for k in _VARIATION_KEYS if k not in v]
        if missing:
            p.append("%s is missing key(s): %s" % (where, ", ".join(missing)))
        unknown = [k for k in v if k not in _VARIATION_KEYS]
        if unknown:
            p.append("%s has unknown key(s): %s (accepted: %s)"
                     % (where, ", ".join(sorted(unknown)), ", ".join(_VARIATION_KEYS)))

        if "name" in v:
            name = v["name"]
            if not isinstance(name, str) or not name:
                p.append("%s name must be a non-empty string, got %r" % (where, name))
            elif not _VARIATION_NAME_RE.fullmatch(name):
                p.append("%s name %r may only contain letters, digits, '_' and '-'; "
                         "it becomes part of a clip directory name" % (where, name))
            elif name in seen:
                p.append("%s name %r is already used by variations[%d]; names must "
                         "be unique, they tell the clips of one scene apart"
                         % (where, name, seen[name]))
            else:
                seen[name] = i

        for k in ("ego_chaos", "actor_chaos"):
            if k not in v:
                continue
            if not _is_number(v[k]):
                p.append("%s %s must be a number between 0.0 and 1.0, got %r"
                         % (where, k, v[k]))
            elif not 0.0 <= v[k] <= 1.0:
                p.append("%s %s must be between 0.0 (calm) and 1.0 (the tuned "
                         "max-chaos settings), got %r" % (where, k, v[k]))
    return p


def _expand_variations(variations):
    """Resolve a preset name to its list. Always returns fresh dicts.

    Copies so that nothing downstream can mutate PRESETS through a returned entry
    and have the next load() see the change.
    """
    if isinstance(variations, str):
        if variations not in PRESETS:
            raise ValueError(_variation_problems(variations)[0])
        variations = PRESETS[variations]
    return [dict(v) for v in variations]




# ---------------------------------------------------------------------------
# Non-chaos generator settings that differ from CaptureConfig's own defaults
# ---------------------------------------------------------------------------
# ★ CaptureConfig's defaults are the pre-tuning values. The tuned config beat
#   several of them, and those wins are not user-facing knobs -- there is no
#   reason to make a user rediscover them. Everything here is a field where
#   configs/max_chaos_15s.json differs from config.py's default for a reason that
#   has nothing to do with the chaos dial.
_TUNED_BASE = {
    # ⚠ These three are set by configs/max_chaos_15s.json and were missing here, so
    # CaptureConfig's own defaults won and chaos=1.0 did NOT reproduce the tuned
    # config for them. Latent only while slowmo_probability is 0.0 -- it becomes
    # real the moment anyone raises it, and they would be capturing against untuned
    # values believing otherwise.
    "slowmo_scale": 0.22,
    "slowmo_lead_s": 0.5,
    "slowmo_hold_s": 2.0,
    # The orchestrated event fires earlier and in a tighter window than the
    # default 0.20-0.40, which leaves more post-event footage inside a 15 s clip.
    "trigger_fraction_min": 0.18,
    "trigger_fraction_max": 0.33,
    # Warmup speed gate, raised with the tuned speed envelope.
    "warmup_min_speed": 4.0,
    # ⚠ jpg, not the config default png. Storage is the binding constraint on a
    # long run (251 MB of PNG-free JPEG frames per 15 s clip as it is), and the
    # frames are dropped after encode unless keep_frames is set. If you set
    # keep_frames you get JPEGs, not lossless PNGs.
    "image_format": "jpg",
    # 30 ego platforms instead of the default 8: cab height, bonnet length and
    # suspension all change the extrinsics and the look of an impact, and ego
    # variety is free.
    "ego_vehicles": [
        "blista", "futo", "premier", "primo", "stanier", "washington",
        "tailgater", "sultan", "buffalo", "dominator", "phoenix", "fugitive",
        "asea", "baller", "granger", "patriot", "seminole", "landstalker",
        "dubsta", "rumpo", "burrito", "minivan", "bison", "sandking", "rebel",
        "pounder", "mule", "benson", "packer", "bodhi2",
    ],
    # Chase rare outcomes harder, and start chasing sooner.
    "outcome_bias": True,
    "outcome_bias_temperature": 2.0,
    "outcome_bias_min_history": 4,
    # clip.mp4 is a hard deliverable.
    # ⚠⚠ FALSE on purpose, and this single flag is load-bearing. With video=True,
    # run_generator.py encodes each kept clip INLINE on the capture thread and then
    # deletes frames/. The Finalizer would then receive a clip with nothing left to
    # encode, find 0 jpgs, and book every GOOD clip as rejected -- measured: 11 good
    # clips, kept=0, all reason "nothing to encode". It also puts ffmpeg back on the
    # capture path, which is exactly what the background finalizer exists to avoid,
    # and makes a --target run unreachable because the kept count never rises.
    "video": False,
    # ⚠ Lifetime chunking is the runner's business, not the settings'. The
    # generator runs until stopped; the supervisor decides when to recycle the
    # game and how many clips a lifetime gets.
    "max_clips": 0,
}


# ---------------------------------------------------------------------------
# Graphics presets
# ---------------------------------------------------------------------------
# GTA V settings.xml element -> value, per quality preset. Element names and the
# value scales are read off a real settings.xml (Documents/Rockstar Games/GTA V).
#
# ⚠ GTA V's own sliders top out at what "ultra" sets here: TextureQuality,
#   ParticleQuality, WaterQuality, ShaderQuality and SSAO cap at 2;
#   ShadowQuality, ReflectionQuality, GrassQuality and Tessellation at 3;
#   AnisotropicFiltering at 16; the LOD scales at 1.0. "ultra" is therefore the
#   real ceiling and "high" is one notch below it, mostly on the distance
#   sliders.
#
# ★ "ultra" reproduces the settings.xml the existing dataset was captured
#   against, element for element. ⚠ Clips captured at different presets are not
#   visually comparable -- LOD distance changes what is on screen, not just how
#   pretty it is. Pick one preset for a dataset and keep it; use "ultra" to match
#   anything already captured.
#
# ⚠ A lower preset raises the pop-in rejection rate. LodScale / MaxLodScale /
#   *LodBias decide how far out geometry streams, and the pop-in gate
#   (popin_photometric_threshold, popin_max_events) rejects clips whose
#   frame-to-frame photometric jump is not explained by camera motion. Cheaper
#   frames, fewer of them kept.
#
# PostFX and DoF sit in this table but are the same at every preset. Bloom, lens
# flare and defocus blur are LENS effects: they are not in the pinhole model the
# exported poses and K describe, so no preset turns them on.
#
# CityDensity is likewise pinned at 1.0 everywhere. It scales the ambient
# population, which is a property of the SCENE, and scene population is the chaos
# dial's job (density_vehicle/ped/parked, seed_*). Letting a graphics preset move
# it would make "low graphics" quietly mean "less traffic" -- a fidelity setting
# silently changing the data.
_GRAPHICS_PRESETS = {
    "low": {
        "ShadowQuality": 0,
        "ReflectionQuality": 0,
        "GrassQuality": 0,
        "TextureQuality": 0,
        "ParticleQuality": 0,
        "WaterQuality": 0,
        "ShaderQuality": 0,
        "SSAO": 0,
        "AnisotropicFiltering": 0,
        "Shadow_SoftShadows": 0,
        "Tessellation": 0,
        "PostFX": 0,
        "DoF": False,
        "LodScale": 0.5,
        "PedLodBias": 0.0,
        "VehicleLodBias": 0.0,
        "MaxLodScale": 0.0,
        "CityDensity": 1.0,
    },
    "medium": {
        "ShadowQuality": 1,
        "ReflectionQuality": 1,
        "GrassQuality": 1,
        "TextureQuality": 1,
        "ParticleQuality": 1,
        "WaterQuality": 1,
        "ShaderQuality": 1,
        "SSAO": 1,
        "AnisotropicFiltering": 4,
        "Shadow_SoftShadows": 1,
        "Tessellation": 1,
        "PostFX": 0,
        "DoF": False,
        "LodScale": 0.7,
        "PedLodBias": 0.4,
        "VehicleLodBias": 0.4,
        "MaxLodScale": 0.3,
        "CityDensity": 1.0,
    },
    "high": {
        "ShadowQuality": 2,
        "ReflectionQuality": 2,
        "GrassQuality": 2,
        "TextureQuality": 2,
        "ParticleQuality": 2,
        "WaterQuality": 2,
        "ShaderQuality": 2,
        "SSAO": 2,
        "AnisotropicFiltering": 8,
        "Shadow_SoftShadows": 3,
        "Tessellation": 2,
        "PostFX": 0,
        "DoF": False,
        "LodScale": 0.9,
        "PedLodBias": 0.7,
        "VehicleLodBias": 0.7,
        "MaxLodScale": 0.6,
        "CityDensity": 1.0,
    },
    "ultra": {
        "ShadowQuality": 3,
        "ReflectionQuality": 3,
        "GrassQuality": 3,
        "TextureQuality": 2,
        "ParticleQuality": 2,
        "WaterQuality": 2,
        "ShaderQuality": 2,
        "SSAO": 2,
        "AnisotropicFiltering": 16,
        "Shadow_SoftShadows": 5,
        "Tessellation": 3,
        "PostFX": 0,
        "DoF": False,
        "LodScale": 1.0,
        "PedLodBias": 1.0,
        "VehicleLodBias": 1.0,
        "MaxLodScale": 1.0,
        "CityDensity": 1.0,
    },
}

#: Presets in quality order, not alphabetical -- this is what error messages
#: list, and "high, low, medium, ultra" reads as though it were a ranking.
GRAPHICS_PRESETS = tuple(_GRAPHICS_PRESETS)


def _xml_value(v):
    """Format a python value the way GTA V writes it in settings.xml."""
    # bool before int: bool IS an int in python, and str(True) is "True", which
    # GTA does not parse.
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return "%.6f" % v
    return str(v)


#: Speed unit suffixes a CLI user may reasonably type. The settings object and
#: every field downstream are m/s; these exist so "30" and "108kph" cannot be
#: confused for each other at the prompt.
_SPEED_UNITS = [
    ("km/h", 1.0 / 3.6), ("kmh", 1.0 / 3.6), ("kph", 1.0 / 3.6),
    ("mph", 0.44704),
    ("m/s", 1.0), ("mps", 1.0), ("ms", 1.0),
]


def parse_ego_speed(spec):
    """Normalise an `ego_speed` value to (low, high) m/s, or None for "auto".

    Accepts what a config file holds (a number, a [low, high] pair, null) and what
    a command line offers ("22", "14-30", "14:30", "80kph", "50mph",
    "auto"). Raises ValueError with a message meant to be shown as-is.
    """
    if spec is None:
        return None
    if isinstance(spec, bool):                      # bool is an int; reject early
        raise ValueError("ego_speed must be a speed or a [low, high] pair, got %r" % (spec,))
    if isinstance(spec, (int, float)):
        lo = hi = float(spec)
    elif isinstance(spec, (list, tuple)):
        if len(spec) != 2 or not all(_is_number(v) for v in spec):
            raise ValueError("ego_speed as a range must be two numbers [low, high], "
                             "got %r" % (spec,))
        lo, hi = float(spec[0]), float(spec[1])
    elif isinstance(spec, str):
        text = spec.strip().lower().replace(" ", "")
        if text in ("", "auto", "none", "off", "default"):
            return None
        scale = 1.0
        for suffix, factor in _SPEED_UNITS:
            if text.endswith(suffix):
                text, scale = text[:-len(suffix)], factor
                break
        # ⚠ Split on the LAST separator, not the first: a lone "-" is also the
        # sign of a negative number, and "14-30" must not become ("", "14", "30").
        parts = re.split(r"[-:]", text.strip("-:"))
        try:
            if len(parts) == 1:
                lo = hi = float(parts[0]) * scale
            elif len(parts) == 2:
                lo, hi = float(parts[0]) * scale, float(parts[1]) * scale
            else:
                raise ValueError
        except ValueError:
            raise ValueError("cannot read %r as a speed; use \"22\" (m/s), "
                             "\"14-30\" for a range, a \"kph\"/\"mph\" suffix, "
                             "or \"auto\" for the chaos-scaled default" % (spec,))
    else:
        raise ValueError("ego_speed must be a number, a [low, high] pair or a string, "
                         "got %r" % (spec,))

    if lo <= 0 or hi <= 0:
        raise ValueError("ego_speed must be greater than 0 m/s, got %r" % (spec,))
    if lo > hi:
        raise ValueError("ego_speed low (%.1f) is above high (%.1f) m/s" % (lo, hi))
    # ⚠ Not a taste limit. The wander task's cruise speed is a target for GTA's AI
    # driver, and above roughly this the driver cannot hold a city road at all --
    # the clip becomes a crash into the first corner, which is not the long tail
    # anyone is asking for. Ask for it deliberately by raising this.
    if hi > 60.0:
        raise ValueError("ego_speed %.1f m/s (%.0f km/h) is beyond what the AI "
                         "driver holds on a road; 60 m/s is the ceiling" % (hi, hi * 3.6))
    return (lo, hi)


@dataclass
class CaptureSettings:
    """Everything a user is expected to set. Two paths are mandatory."""

    gta_dir: str
    output_dir: str
    width: int = 1920
    height: int = 1080
    rate_hz: int = 30
    clip_duration_s: float = 15.0
    discard_lead_s: float = 2.0
    target_clips: int = 0          # 0 = run until stopped
    chaos: float = 0.8             # 0.0..1.0

    #: Ego speed command, m/s. None/"auto" leaves it to `chaos` (the EGO table
    #: below interpolates a 10-18 m/s envelope at calm to 16-34 at full chaos).
    #: A number pins every clip to that speed; [low, high] samples the range.
    #: ★ An explicit value also makes the pipeline ENFORCE it: the velocity is
    #: re-asserted at the first recorded frame rather than only at spawn, so the
    #: clip starts at the speed that was asked for instead of whatever survived
    #: the discard lead. Measured without it: the first recorded frame ran at a
    #: median 0.49x the commanded speed.
    #: ⚠ It commands the ENTRY speed and the AI driver's cruise target, not the
    #: whole clip. Restraint bits, driver aggressiveness, traffic and corners all
    #: still apply afterwards, and a lawful ego at 30 m/s will still stop at a red.
    ego_speed: object = None
    graphics: str = "high"         # "low"|"medium"|"high"|"ultra"
    video_crf: int = 16
    keep_frames: bool = False
    max_hours: float = 0.0         # 0 = unlimited
    clips_per_lifetime: int = 12
    host: str = "172.28.32.1"      # WSL->Windows vEthernet address
    port: int = 8000

    #: How to start the game. The default is Steam's "Grand Theft Auto V Legacy".
    #: ⚠ Legacy is NOT the same product as "Grand Theft Auto V Enhanced" (Steam app
    #: 3240220), which installs to its own folder and is not supported by the
    #: ScriptHookV build this plugin targets. For a Rockstar or Epic copy, point this
    #: at that launcher's URI or directly at PlayGTAV.exe.
    launch_command: str = "steam://rungameid/271590"

    #: Counterfactual capture: the same scene captured once per entry, only the
    #: ego and actor behaviour differing between the clips. [] = off, one clip per
    #: location as before. Either a list of {"name", "ego_chaos", "actor_chaos"}
    #: objects or a preset name from PRESETS ("counterfactual" is the 2x2
    #: sane/chaotic grid), which load() expands. See "Counterfactual variations"
    #: above for what is and is not held constant.
    variations: list = field(default_factory=list)
    #: Reproducible scenes: clear the game's ambient population and seed every actor
    #: from the scene seed. "auto" = on whenever variations are in use (the whole
    #: point of a variation is that the scene is the same), off for single clips so
    #: they keep background traffic. true/false forces it either way.
    #: ⚠ Physics and AI remain non-deterministic across runs: same situation, not
    #: same pixels.
    deterministic_population: object = "auto"

    # -- construction -------------------------------------------------------

    @classmethod
    def load(cls, path):
        """Read a capture.json. Unknown keys are an error, not a warning.

        ⚠ A typo'd key that is silently ignored is the worst possible failure
        here: the run starts, looks healthy, and produces a dataset captured with
        a setting the user believes they changed. Nothing downstream can detect
        it after the fact.
        """
        try:
            with open(path) as f:
                raw = json.load(f)
        except IOError as exc:
            raise ValueError("cannot read config %s: %s" % (path, exc))
        except ValueError as exc:
            raise ValueError("config %s is not valid JSON: %s" % (path, exc))

        if not isinstance(raw, dict):
            raise ValueError("config %s must be a JSON object, got %s"
                             % (path, type(raw).__name__))

        known = [f.name for f in fields(cls)]
        unknown = [k for k in raw if k not in known]
        if unknown:
            lines = ["config %s has %d unknown key(s):" % (path, len(unknown))]
            for k in sorted(unknown):
                near = difflib.get_close_matches(k, known, n=1, cutoff=0.6)
                lines.append("  %r%s" % (k, "   did you mean %r?" % near[0] if near else ""))
            lines.append("accepted keys: %s" % ", ".join(known))
            raise ValueError("\n".join(lines))

        missing = [n for n in ("gta_dir", "output_dir") if n not in raw]
        if missing:
            raise ValueError("config %s is missing required key(s): %s"
                             % (path, ", ".join(missing)))

        # A preset name stands in for its list. ⚠ Fatal rather than a validate()
        # line, like an unknown key: this is the one place the expansion happens,
        # and an object holding an unresolvable name is not a settings object.
        if isinstance(raw.get("variations"), str):
            try:
                raw["variations"] = _expand_variations(raw["variations"])
            except ValueError as exc:
                raise ValueError("config %s: %s" % (path, exc))
        return cls(**raw)

    # -- checking -----------------------------------------------------------

    def validate(self):
        """Return a list of human-readable problems. Empty list means OK."""
        p = []  # type: List[str]

        dp = self.deterministic_population
        if not (isinstance(dp, bool) or (isinstance(dp, str) and dp.lower() == "auto")):
            p.append('deterministic_population must be true, false or "auto", got %r' % (dp,))

        # ⚠ capture.example.json marks both paths with this. JSON has no comments,
        # so the marker lives in the value, and a value that survives into a real
        # run means the user copied the example and never edited it -- catch it
        # here rather than let 400 GB land in a directory called "EDIT ME".
        for name in ("gta_dir", "output_dir"):
            v = getattr(self, name)
            if isinstance(v, str) and "EDIT ME" in v.upper():
                p.append("%s is still the placeholder from capture.example.json; "
                         "set it to a real path" % name)

        # --- game install ---
        if not isinstance(self.gta_dir, str) or not self.gta_dir.strip():
            p.append("gta_dir is empty; set it to the folder containing GTA5.exe, "
                     "e.g. \"D:/SteamLibrary/steamapps/common/Grand Theft Auto V\"")
        elif "EDIT ME" in self.gta_dir.upper():
            pass                                   # already reported above
        elif not os.path.isdir(self.gta_dir):
            p.append("gta_dir %r does not exist (or is not a directory); it must be "
                     "the folder containing GTA5.exe, reachable from WSL as a /mnt "
                     "path or as a Windows path" % self.gta_dir)
        else:
            # Legacy and Enhanced ship different exe names; setup_display.sh
            # already knows about both.
            exes = ("GTA5.exe", "GTA5_Enhanced.exe")
            if not any(os.path.isfile(os.path.join(self.gta_dir, e)) for e in exes):
                p.append("gta_dir %r contains no %s; point it at the game folder "
                         "itself, not its parent" % (self.gta_dir, " or ".join(exes)))

        # --- output ---
        if not isinstance(self.output_dir, str) or not self.output_dir.strip():
            p.append("output_dir is empty; set it to a directory the run may create "
                     "and write clips into")
        elif "EDIT ME" in self.output_dir.upper():
            pass                                   # already reported above
        else:
            probe = os.path.abspath(self.output_dir)
            if os.path.exists(probe) and not os.path.isdir(probe):
                p.append("output_dir %r exists and is not a directory" % self.output_dir)
            else:
                # The leaf is normally created by the run, so its absence is fine.
                # An unwritable nearest-existing ancestor is not.
                while not os.path.exists(probe):
                    parent = os.path.dirname(probe)
                    if parent == probe:
                        break
                    probe = parent
                if not os.path.isdir(probe):
                    p.append("output_dir %r cannot be created: no existing parent "
                             "directory" % self.output_dir)
                elif not os.access(probe, os.W_OK):
                    p.append("output_dir %r is not writable (nearest existing parent "
                             "is %r); pick another location or fix its permissions"
                             % (self.output_dir, probe))

        # --- image shape ---
        for name in ("width", "height"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool):
                p.append("%s must be a whole number, got %r" % (name, v))
            elif v <= 0:
                p.append("%s must be positive, got %d" % (name, v))
            elif v % 2:
                # ⚠ H.264 yuv420p subsamples chroma 2x2; an odd dimension either
                # fails the encode or gets silently padded, and a padded frame no
                # longer matches the K in meta.json.
                p.append("%s must be even (H.264 yuv420p needs even dimensions), "
                         "got %d" % (name, v))

        # --- timing ---
        if not isinstance(self.rate_hz, int) or isinstance(self.rate_hz, bool):
            p.append("rate_hz must be a whole number, got %r" % (self.rate_hz,))
        elif not 1 <= self.rate_hz <= 60:
            p.append("rate_hz must be between 1 and 60, got %d; note it is advisory "
                     "anyway -- the plugin pauses per frame and real sampling ranges "
                     "~14-35 Hz, which is why every frame carries its own "
                     "game_time_ms" % self.rate_hz)

        if not _is_number(self.clip_duration_s):
            p.append("clip_duration_s must be a number, got %r" % (self.clip_duration_s,))
        elif self.clip_duration_s <= 0:
            p.append("clip_duration_s must be greater than 0, got %r"
                     % (self.clip_duration_s,))

        if not _is_number(self.discard_lead_s):
            p.append("discard_lead_s must be a number, got %r" % (self.discard_lead_s,))
        elif self.discard_lead_s < 0:
            p.append("discard_lead_s must be 0 or more, got %r" % (self.discard_lead_s,))
        elif _is_number(self.clip_duration_s) and self.discard_lead_s >= self.clip_duration_s:
            p.append("discard_lead_s (%r) is not shorter than clip_duration_s (%r); "
                     "the lead is captured and thrown away, so nothing would be kept"
                     % (self.discard_lead_s, self.clip_duration_s))

        # --- chaos / graphics ---
        if not _is_number(self.chaos):
            p.append("chaos must be a number between 0.0 and 1.0, got %r" % (self.chaos,))
        elif not 0.0 <= self.chaos <= 1.0:
            p.append("chaos must be between 0.0 (calm, lawful, sparse) and 1.0 "
                     "(the tuned max-chaos settings), got %r" % (self.chaos,))

        try:
            parse_ego_speed(self.ego_speed)
        except ValueError as exc:
            p.append(str(exc))

        if self.graphics not in _GRAPHICS_PRESETS:
            p.append("graphics %r is not a known preset; use one of: %s"
                     % (self.graphics, ", ".join(GRAPHICS_PRESETS)))

        # --- encode ---
        if not isinstance(self.video_crf, int) or isinstance(self.video_crf, bool):
            p.append("video_crf must be a whole number, got %r" % (self.video_crf,))
        elif not 0 <= self.video_crf <= 51:
            p.append("video_crf must be between 0 (lossless, huge) and 51 (worst); "
                     "16 is visually near-lossless, got %r" % (self.video_crf,))

        # --- run control ---
        if not isinstance(self.target_clips, int) or isinstance(self.target_clips, bool):
            p.append("target_clips must be a whole number (0 = run until stopped), "
                     "got %r" % (self.target_clips,))
        elif self.target_clips < 0:
            p.append("target_clips must be 0 or more (0 = run until stopped), got %d"
                     % self.target_clips)

        if not _is_number(self.max_hours):
            p.append("max_hours must be a number (0 = unlimited), got %r" % (self.max_hours,))
        elif self.max_hours < 0:
            p.append("max_hours must be 0 or more (0 = unlimited), got %r" % (self.max_hours,))

        if not isinstance(self.clips_per_lifetime, int) or isinstance(self.clips_per_lifetime, bool):
            p.append("clips_per_lifetime must be a whole number, got %r"
                     % (self.clips_per_lifetime,))
        elif self.clips_per_lifetime < 1:
            p.append("clips_per_lifetime must be at least 1; it is how many clips one "
                     "game process captures before it is recycled, got %d"
                     % self.clips_per_lifetime)

        # --- link ---
        if not isinstance(self.host, str) or not self.host.strip():
            p.append("host is empty; from WSL this is the Windows-side vEthernet "
                     "address, e.g. \"172.28.32.1\" (ip route show | grep default)")
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            p.append("port must be a whole number, got %r" % (self.port,))
        elif not 1 <= self.port <= 65535:
            p.append("port must be between 1 and 65535, got %d" % self.port)


        # --- variations ---
        p.extend(_variation_problems(self.variations))

        return p

    # -- derived views ------------------------------------------------------

    def to_generator_config(self, ego_chaos=None, actor_chaos=None):
        """Build the dict `longtail.config.CaptureConfig(**d)` accepts.

        `ego_chaos` / `actor_chaos` set the dial for the EGO / ACTOR knob groups;
        None means "use self.chaos", so a call with no arguments is the single-dial
        config. SCENE knobs always come from self.chaos: two configs from the same
        settings differ only in behaviour, which is what makes them variations of
        one scene rather than two scenes.
        """
        d = dict(_TUNED_BASE)                     # type: Dict[str, Any]
        d.update(_scale_knobs(_SCENE, self.chaos))
        d.update(_scale_knobs(_EGO, self.chaos if ego_chaos is None else ego_chaos))
        d.update(_scale_knobs(_ACTOR, self.chaos if actor_chaos is None else actor_chaos))

        # ★ An explicit ego_speed overrides the EGO table AFTER the interpolation,
        #   for every variation. That is the point of it: with speed pinned, a
        #   scene's counterfactuals differ in behaviour at a held speed, so speed
        #   is a control variable instead of another thing chaos moved.
        span = parse_ego_speed(self.ego_speed)
        if span is not None:
            lo, hi = span
            d["ego_speed_min"] = lo
            d["ego_speed_max"] = hi
            # The cruise target the AI driver aims for between events. Midpoint of
            # the range; the same number when it is a pin.
            d["ego_cruise_speed"] = (lo + hi) / 2.0
            # Tells the capture client the speed was ASKED for rather than sampled,
            # which is what licenses re-asserting it at the first recorded frame.
            d["ego_speed_enforce"] = True

        dp = self.deterministic_population
        if isinstance(dp, str) and dp.lower() == "auto":
            dp = bool(self.variations)
        d["deterministic_population"] = bool(dp)

        d["width"] = int(self.width)
        d["height"] = int(self.height)
        # The plugin resamples the backbuffer to the requested frame size, so
        # asking for a screen resolution that differs from the frame size buys a
        # rescale and nothing else. Keep them equal.
        d["screen_width"] = int(self.width)
        d["screen_height"] = int(self.height)

        d["rate_hz"] = int(self.rate_hz)
        # A fixed-length dataset: min and max are the same request.
        d["clip_seconds_min"] = float(self.clip_duration_s)
        d["clip_seconds_max"] = float(self.clip_duration_s)
        d["discard_lead_s"] = float(self.discard_lead_s)

        d["video_crf"] = int(self.video_crf)
        # ⚠ video_only is "drop the frames after a successful encode", so it is
        # the inverse of keep_frames. writer.encode_video returns None on a
        # frame/pose count mismatch and the caller must not prune on None -- that
        # gate lives in the runner, not here.
        d["video_only"] = not bool(self.keep_frames)

        d["out_dir"] = self.output_dir
        return d

    def variation_configs(self):
        """[(variation, generator_config), ...], one per entry of `variations`.

        [] when variations are off. Each generator_config is a complete
        to_generator_config() dict, so any one of them can go to CaptureConfig(**d)
        exactly as the single-dial config does. ⚠ The variation and its dials are
        deliberately NOT inside that dict: CaptureConfig rejects unknown fields.
        They travel beside it -- in the "variation_plan" the runner writes into the
        generator's JSON config, and from there into each clip's meta.json.
        """
        out = []
        for v in _expand_variations(self.variations):
            variation = {
                "name": v["name"],
                "ego_chaos": float(v["ego_chaos"]),
                "actor_chaos": float(v["actor_chaos"]),
            }
            out.append((variation, self.to_generator_config(
                ego_chaos=variation["ego_chaos"], actor_chaos=variation["actor_chaos"])))
        return out

    def graphics_xml_values(self):
        """settings.xml element name -> value string, for this quality preset."""
        preset = _GRAPHICS_PRESETS.get(self.graphics)
        if preset is None:
            raise ValueError("unknown graphics preset %r; use one of: %s"
                             % (self.graphics, ", ".join(GRAPHICS_PRESETS)))

        out = {}
        for name, value in preset.items():
            out[name] = _xml_value(value)

        # ★ Forced regardless of preset. Each of these breaks capture rather than
        # merely making it prettier or slower:
        #   MSAA               -- multisampling averages sub-pixel samples taken at
        #                         different image-plane positions, so an edge pixel
        #                         no longer corresponds to one ray through the
        #                         pinhole the exported K and pose describe.
        #   MotionBlurStrength -- smears a frame with the ones around it in time;
        #                         the pose is an instant, the pixels are not.
        #   VSync              -- capture blocks on Present, so vsync throttles the
        #                         whole pipeline to the refresh rate for nothing.
        #   Windowed=1         -- NOT borderless. On a DPI-aware app borderless
        #                         takes the entire panel, which is more pixels to
        #                         read back per frame with no gain (measured on a
        #                         2560x1440 panel: 1.8x the pixels).
        #   PauseOnFocusLoss   -- the game freezes the moment the window loses
        #                         focus, and the capture waits forever on a paused
        #                         game while every liveness check still passes.
        out["ScreenWidth"] = str(int(self.width))
        out["ScreenHeight"] = str(int(self.height))
        out["MSAA"] = "0"
        out["MotionBlurStrength"] = "%.6f" % 0.0
        out["VSync"] = "0"
        out["Windowed"] = "1"
        out["PauseOnFocusLoss"] = "0"
        return out


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)
