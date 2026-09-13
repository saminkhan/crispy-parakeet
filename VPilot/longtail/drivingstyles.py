"""GTA V driving-style bitmasks.

⚠ These are community-documented values, not an official API. Decompose and
verify against NativeDB for your game build before trusting a preset -- a wrong
bit here does not error, it just quietly produces a boring clip.

The style bitmask is the main lever for ego-caused long-tail events: running a
red light and crossing the centre line are *style flags*, not scripted paths.
"""

STOP_BEFORE_VEHICLES = 1
STOP_BEFORE_PEDS = 2
AVOID_VEHICLES = 4
AVOID_EMPTY_VEHICLES = 8
AVOID_PEDS = 16
AVOID_OBJECTS = 32
STOP_AT_TRAFFIC_LIGHTS = 128
USE_BLINKERS = 256
ALLOW_WRONG_WAY = 512
GO_IN_REVERSE = 1024
TAKE_SHORTEST_PATH = 262144
IGNORE_ROADS = 4194304
IGNORE_ALL_PATHING = 16777216

_FLAG_NAMES = {v: k for k, v in globals().items() if isinstance(v, int) and not k.startswith("_")}

# Composite presets in common use.
NORMAL = 786603            # obeys lights, avoids collisions -- the "boring" baseline
RUSHED = 786469
AVOID_TRAFFIC = 786468
AVOID_TRAFFIC_EXTREMELY = 6
IGNORE_LIGHTS = 2          # stops for peds only: blows through intersections
PLOUGH_THROUGH = 4         # avoids vehicles only: no lights, no peds, no lanes
STRAIGHT_LINE = IGNORE_ALL_PATHING   # what upstream hard-codes for spawned cars

#: Styles that make the ego itself misbehave, for ego-caused events.
AGGRESSIVE_PRESETS = {
    "ignore_lights": IGNORE_LIGHTS,
    "plough_through": PLOUGH_THROUGH,
    "wrong_way": AVOID_TRAFFIC_EXTREMELY | ALLOW_WRONG_WAY,
    "shortest_path_no_lights": TAKE_SHORTEST_PATH | AVOID_VEHICLES,
}


def decompose(mode):
    """Return the list of known flags set in `mode`, plus any unrecognised bits.

    Use this to sanity-check a preset before you burn a night of capture on it:
        >>> decompose(786603)
    """
    known, rest = [], int(mode)
    for bit in sorted(_FLAG_NAMES, reverse=True):
        if bit and rest & bit == bit:
            known.append(_FLAG_NAMES[bit])
            rest &= ~bit
    return {"flags": known, "unrecognised_bits": rest}


if __name__ == "__main__":
    for name in ("NORMAL", "RUSHED", "AVOID_TRAFFIC_EXTREMELY", "IGNORE_LIGHTS",
                 "PLOUGH_THROUGH", "STRAIGHT_LINE"):
        print(f"{name:26s} {globals()[name]:>10d}  {decompose(globals()[name])}")


#: Bits whose *presence* restrains the driver. Sampling these independently gives
#: a smooth gradient between "obeys everything" and "ignores everything" instead
#: of a handful of discrete presets.
RESTRAINT_BITS = [
    STOP_BEFORE_VEHICLES, STOP_BEFORE_PEDS, AVOID_VEHICLES, AVOID_EMPTY_VEHICLES,
    AVOID_PEDS, AVOID_OBJECTS, STOP_AT_TRAFFIC_LIGHTS, USE_BLINKERS,
]

#: Bits that release the driver from the road network entirely.
#:
#: ⚠ These are NOT a chaos knob. With either bit set the wander task has no
#: obligation to the node graph at all, so the ego drives up an embankment and
#: keeps going -- 15 s of dirt, with every other frame-level signal (collisions,
#: traffic, peds) reading as a perfectly healthy clip. They are excluded from
#: sampling by default and masked out again plugin-side in
#: `Scenario::egoDrivingModeOnRoad()`; belt and braces, because a style that
#: leaves the network produces footage that looks fine until you watch it.
OFFROAD_BITS = [IGNORE_ROADS, IGNORE_ALL_PATHING]

#: Bits whose presence *removes* restraint without leaving the road network.
#: Wrong-way and shortest-path are on-road misbehaviour -- exactly the long tail
#: we want. IGNORE_ROADS used to live here; see OFFROAD_BITS for why it does not.
LICENSE_BITS = [ALLOW_WRONG_WAY, TAKE_SHORTEST_PATH]

_OFFROAD_MASK = 0
for _b in OFFROAD_BITS:
    _OFFROAD_MASK |= _b


def sanitize(mode, allow_offroad=False):
    """Strip the off-network bits from a style bitmask.

    Call this on anything headed for the ego -- presets, config overrides, and
    hand-written masks included. `AGGRESSIVE_PRESETS["..."]` is safe today but
    nothing stops a future preset from picking up IGNORE_ROADS.
    """
    mode = int(mode)
    if allow_offroad or mode < 0:
        return mode
    return mode & ~_OFFROAD_MASK


def sample(rng, p_restraint=0.35, p_license=0.25, allow_offroad=False):
    """Sample a driving style bit-by-bit. Returns (mode, description).

    Off-network bits are never sampled unless `allow_offroad` is set: see
    OFFROAD_BITS.
    """
    mode = 0
    kept = []
    for bit in RESTRAINT_BITS:
        if rng.random() < p_restraint:
            mode |= bit
            kept.append(_FLAG_NAMES[bit])
    for bit in LICENSE_BITS:
        if rng.random() < p_license:
            mode |= bit
            kept.append(_FLAG_NAMES[bit])
    if allow_offroad:
        for bit in OFFROAD_BITS:
            if rng.random() < p_license:
                mode |= bit
                kept.append(_FLAG_NAMES[bit])
    return sanitize(mode, allow_offroad), kept


def restraint_score(mode):
    """0.0 = ignores everything, 1.0 = obeys everything. A usable difficulty axis."""
    return sum(1 for b in RESTRAINT_BITS if mode & b) / float(len(RESTRAINT_BITS))
