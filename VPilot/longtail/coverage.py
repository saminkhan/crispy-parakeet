"""Map coverage sampler: maximise scene variation across the whole GTA V world.

Two things this fixes relative to the naive approach:

1. ⚠ The archived 2017 Waterloo fork sampled x,y ~ U[-2500, 2500]. That misses
   the entire north of the map -- Paleto Bay, Mount Chiliad, the northern
   freeways -- which is exactly the long-range highway content you would most
   want. Bounds here cover the playable world.

2. ★ Uniform XY over-samples empty desert and ocean and under-samples the dense
   city, where interactions need traffic to interact with. So we stratify by
   region with per-region quotas and round-robin between them, rather than
   sampling i.i.d.

We cannot query the road graph from Python -- there is no native access here.
Instead we propose an (x, y) and let the plugin snap it: `buildScenario()` calls
`GET_CLOSEST_VEHICLE_NODE_WITH_HEADING`, so any proposal lands on a road. The
ledger records the *snapped* position the plugin reports back, which is what
d_min separation is enforced against.
"""

import json
import math
import os
import random

#: Approximate region boxes (x_min, x_max, y_min, y_max) and a sampling weight.
#: ⚠ Hand-drawn from the map, not authoritative -- widen or split them freely.
REGIONS = {
    # ⚠ Weighted hard toward urban. SET_VEHICLE_DENSITY_MULTIPLIER_THIS_FRAME
    # SCALES GTA's existing population model -- it does not invent traffic where
    # the game spawns none. Measured: west_highway at 2.73x density yielded ONE
    # visible vehicle, because a canyon road's base population is ~0 and 3x of
    # nothing is nothing. Chaos needs third parties to be chaotic with.
    #
    # Rural regions are kept but heavily down-weighted rather than removed: the
    # scripted event still fires there (scenarios spawn their own participants),
    # so they contribute scenery and road-type variety, just not ambient density.
    "ls_downtown":    (-800.0,   500.0, -1200.0,   200.0, 10.0),
    "ls_east_beach":  (   0.0,  1600.0, -2200.0,   200.0,  7.0),
    "ls_south_port":  (-1400.0, 1200.0, -3300.0, -1200.0,  6.0),
    "vinewood_hills": (-2000.0,  1000.0,   200.0,  1200.0,  4.0),
    "north_freeway":  ( -900.0,  1200.0,  1000.0,  4200.0,  3.0),
    "west_highway":   (-3300.0, -1200.0,  -600.0,  4000.0,  2.0),
    "sandy_shores":   ( 1000.0,  3000.0,  2600.0,  4200.0,  1.0),
    "grapeseed":      ( 1400.0,  3000.0,  4200.0,  5600.0,  0.5),
    "paleto_bay":     (-1000.0,   500.0,  5800.0,  7000.0,  0.5),
    "mt_chiliad":     (-1000.0,  1000.0,  4200.0,  5800.0,  0.25),
    "far_north_road": (-2000.0,  2000.0,  6800.0,  7900.0,  0.25),
}


class CoverageSampler:
    """Round-robin over weighted regions with a d_min separation constraint.

    The ledger persists to disk, so an interrupted run resumes without
    re-covering ground -- which is what makes "run until stopped" cheap.
    """

    def __init__(self, ledger_path, d_min=140.0, seed=0, regions=None):
        self.ledger_path = ledger_path
        self.d_min = float(d_min)
        self.rng = random.Random(seed)
        self.regions = dict(regions or REGIONS)
        self.visited = []          # list of (x, y, region)
        self.region_counts = {r: 0 for r in self.regions}
        self._load()
        # Deterministic weighted round-robin schedule rather than i.i.d. draws:
        # i.i.d. leaves clumps and holes at the sample counts a real run reaches.
        self._schedule = []
        for name, spec in self.regions.items():
            self._schedule.extend([name] * max(1, int(round(spec[4] * 2))))
        self.rng.shuffle(self._schedule)
        self._cursor = 0

    # ------------------------------------------------------------------
    def _load(self):
        if os.path.exists(self.ledger_path):
            with open(self.ledger_path) as f:
                data = json.load(f)
            self.visited = [tuple(v) for v in data.get("visited", [])]
            self.region_counts.update(data.get("region_counts", {}))

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.ledger_path)), exist_ok=True)
        with open(self.ledger_path, "w") as f:
            json.dump({"visited": self.visited, "region_counts": self.region_counts}, f)

    # ------------------------------------------------------------------
    def _too_close(self, x, y):
        d2 = self.d_min * self.d_min
        for vx, vy, _ in self.visited:
            if (vx - x) ** 2 + (vy - y) ** 2 < d2:
                return True
        return False

    def next_region(self):
        name = self._schedule[self._cursor % len(self._schedule)]
        self._cursor += 1
        return name

    def propose(self, max_tries=200):
        """Return (x, y, region_name) for the next clip.

        Falls back to the least-covered region if the scheduled one is saturated,
        and finally relaxes d_min rather than deadlocking -- a generator that
        stops proposing is worse than one that occasionally repeats a location.
        """
        for _ in range(max_tries):
            region = self.next_region()
            x0, x1, y0, y1, _w = self.regions[region]
            x = self.rng.uniform(x0, x1)
            y = self.rng.uniform(y0, y1)
            if not self._too_close(x, y):
                return x, y, region

        region = min(self.region_counts, key=lambda r: self.region_counts[r])
        x0, x1, y0, y1, _w = self.regions[region]
        return self.rng.uniform(x0, x1), self.rng.uniform(y0, y1), region

    def record(self, x, y, region):
        """Record the position the plugin actually snapped to."""
        self.visited.append((float(x), float(y), region))
        self.region_counts[region] = self.region_counts.get(region, 0) + 1

    # ------------------------------------------------------------------
    def stats(self):
        total = sum(self.region_counts.values())
        return {
            "total_clips": total,
            "regions_touched": sum(1 for v in self.region_counts.values() if v),
            "region_counts": dict(sorted(self.region_counts.items(), key=lambda kv: -kv[1])),
            "spread_metres": self._spread(),
        }

    def _spread(self):
        if len(self.visited) < 2:
            return 0.0
        xs = [v[0] for v in self.visited]
        ys = [v[1] for v in self.visited]
        return math.hypot(max(xs) - min(xs), max(ys) - min(ys))
