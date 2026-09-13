"""Outcome detection and outcome-biased scenario selection.

★ The point of this module: **setup entropy is not outcome entropy.** You can
randomise sixty knobs and still get 85% of clips where the ego drove past and
nothing happened, because whether an event lands depends on execution, not
configuration. Without outcome labels the generator records what it *asked for*
and never what *occurred*, so "maximise long-tail entropy" is unmeasurable.

Two pieces:
  OutcomeLabeler  -- per-clip label from the per-frame ego state stream
  OutcomeBiased   -- scenario selection that chases under-represented outcomes
"""

import json
import math
import os

NO_EVENT = "no_event"
NEAR_MISS = "near_miss"
MINOR_CONTACT = "minor_contact"
MAJOR_COLLISION = "major_collision"
ROLLOVER = "rollover"
FIRE = "fire"

#: Ordered most severe first. A clip gets the most severe label it qualifies for.
LABELS = [FIRE, ROLLOVER, MAJOR_COLLISION, MINOR_CONTACT, NEAR_MISS, NO_EVENT]


class OutcomeLabeler:
    """Label a clip from its per-frame ego state.

    Thresholds are deliberately explicit rather than tuned: they are the
    definition of the label, and every clip records the measurements alongside
    the label so a downstream consumer can re-threshold without re-capturing.
    """

    def __init__(self,
                 body_health_major=200.0,     # drop in GET_VEHICLE_BODY_HEALTH
                 body_health_minor=8.0,
                 decel_major=9.0,             # m/s^2, sustained over one frame pair
                 decel_near_miss=5.0,
                 roll_deg=70.0):
        self.body_health_major = body_health_major
        self.body_health_minor = body_health_minor
        self.decel_major = decel_major
        self.decel_near_miss = decel_near_miss
        self.roll_deg = roll_deg

    def measure(self, frames):
        """frames: list of dicts with keys from the plugin's ego state export."""
        if not frames:
            return {"label": NO_EVENT, "reason": "no frames"}

        def g(f, k, default=0.0):
            v = f.get(k, default)
            return default if v is None else v

        body0 = g(frames[0], "body_health", 1000.0)
        eng0 = g(frames[0], "engine_health", 1000.0)
        body_min = min(g(f, "body_health", 1000.0) for f in frames)
        eng_min = min(g(f, "engine_health", 1000.0) for f in frames)
        tank_min = min(g(f, "tank_health", 1000.0) for f in frames)

        on_fire = any(bool(f.get("on_fire")) for f in frames)
        on_roof = any(bool(f.get("on_roof")) for f in frames)
        collided = any(bool(f.get("collided")) for f in frames)
        max_roll = max(abs(g(f, "roll", 0.0)) for f in frames)

        # Peak deceleration from the speed trace, using the engine clock.
        #
        # ⚠ This previously skipped only dt <= 1e-3, which is nowhere near enough.
        # Capture pauses the game every frame, so intervals are non-uniform and
        # occasional 1-3 ms gaps appear. A normal 1 m/s change across 2 ms reads as
        # 500 m/s^2 -- 50 g -- and that bogus value then ESCALATED a 23-point scrape
        # to "major_collision" via the decel_major rule. A real impact is tens of g
        # at most, and a sample interval below ~20 ms is a timing artifact rather
        # than a measurement.
        #
        # Estimate over a window of >= MIN_WINDOW_S instead of adjacent frames, so
        # the figure is a physical deceleration rather than frame-clock noise.
        MIN_WINDOW_S = 0.05
        peak_decel = 0.0
        skipped = 0
        j0 = 0
        for i in range(len(frames)):
            for j in range(max(i + 1, j0), len(frames)):
                dt = (g(frames[j], "game_time_ms") - g(frames[i], "game_time_ms")) / 1000.0
                if dt < MIN_WINDOW_S:
                    continue
                dv = g(frames[j], "speed") - g(frames[i], "speed")
                peak_decel = max(peak_decel, -dv / dt)
                break
            else:
                skipped += 1

        m = {
            "body_health_drop": round(body0 - body_min, 2),
            "engine_health_drop": round(eng0 - eng_min, 2),
            "tank_health_min": round(tank_min, 2),
            "peak_decel_mps2": round(peak_decel, 3),
            "decel_windows_unusable": skipped,
            "max_abs_roll_deg": round(max_roll, 2),
            "collided": collided,
            "on_fire": on_fire,
            "on_roof": on_roof,
            "max_speed_mps": round(max(g(f, "speed") for f in frames), 2),

            # ★ Traffic reaction. Everything above is about the EGO, so "do the
            # other vehicles actually react" was unanswerable from the data and
            # could only be judged by watching clips. These make it a number, and
            # they are the metric to watch when tuning NPC aggression.
            "others_damaged_max": max(int(g(f, "nearby_damaged")) for f in frames),
            "others_on_fire_max": max(int(g(f, "nearby_on_fire")) for f in frames),
            "others_wrecked_max": max(int(g(f, "nearby_wrecked")) for f in frames),
            "others_damaged_delta": (max(int(g(f, "nearby_damaged")) for f in frames)
                                     - int(g(frames[0], "nearby_damaged"))),
            "nearby_vehicles_max": max(int(g(f, "nearby_vehicles")) for f in frames),
            "nearby_vehicles_median": sorted(
                int(g(f, "nearby_vehicles")) for f in frames)[len(frames) // 2],
            "others_max_speed_mps": round(max(g(f, "nearby_max_speed") for f in frames), 2),
            # ⚠ The number that decides the ignition threshold. Guessing it gave
            # zero fires across 26 clips, which told us nothing.
            "others_min_body_health": round(
                min(g(f, "nearby_min_body_health", 1000.0) for f in frames), 1),
        }
        m["label"] = self._label(m)
        return m

    def _label(self, m):
        if m["on_fire"]:
            return FIRE
        if m["on_roof"] or m["max_abs_roll_deg"] > self.roll_deg:
            return ROLLOVER

        # ⚠ Deceleration alone must NOT imply a collision. Emergency braking is
        # ~9 m/s^2 with no contact at all, and labelling that MAJOR_COLLISION
        # both mislabels the clip and starves the near-miss class -- which is the
        # class real fleet logs are most short of, and the reason for this
        # generator. Severity is gated on damage; deceleration only escalates a
        # contact that already happened.
        if m["body_health_drop"] >= self.body_health_major:
            return MAJOR_COLLISION
        if m["collided"]:
            if (m["body_health_drop"] >= self.body_health_minor
                    and m["peak_decel_mps2"] >= self.decel_major):
                return MAJOR_COLLISION
            if m["body_health_drop"] >= self.body_health_minor:
                return MINOR_CONTACT
            # Contact with no measurable damage (kerb, scrape) still counts as
            # contact only if it cost speed; otherwise it is ambient noise --
            # HAS_ENTITY_COLLIDED_WITH_ANYTHING fires on trivial surface contact.
            if m["peak_decel_mps2"] >= self.decel_near_miss:
                return MINOR_CONTACT
        if m["peak_decel_mps2"] >= self.decel_near_miss:
            return NEAR_MISS
        return NO_EVENT


class OutcomeBiased:
    """Pick scenarios so that rare *outcomes* get produced, not rare setups.

    Keeps P(outcome | scenario) from observed history and scores each scenario by
    the rarity it is expected to yield:

        score(s) = sum_o  P(o|s) / (1 + count(o))

    With a Laplace prior, so an unobserved scenario is optimistic rather than
    starved. Falls back to the scenario's static weight until it has history.

    Persists alongside the coverage ledger so a restarted run keeps chasing the
    same gaps rather than re-flattening its own histogram.
    """

    def __init__(self, path, prior=0.5, enabled=True, temperature=1.0, min_history=8):
        self.path = path
        self.prior = prior
        self.enabled = enabled
        #: >1 chases rare outcomes harder, <1 softens toward uniform.
        self.temperature = temperature
        #: Below this many clips a scenario keeps its static weight (exploration).
        self.min_history = min_history
        self.by_scenario = {}     # name -> {label: count}
        self.totals = {l: 0 for l in LABELS}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                d = json.load(f)
            self.by_scenario = d.get("by_scenario", {})
            self.totals.update(d.get("totals", {}))

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"by_scenario": self.by_scenario, "totals": self.totals}, f, indent=2)

    def record(self, scenario_name, label):
        self.by_scenario.setdefault(scenario_name, {l: 0 for l in LABELS})
        self.by_scenario[scenario_name][label] = \
            self.by_scenario[scenario_name].get(label, 0) + 1
        self.totals[label] = self.totals.get(label, 0) + 1

    def score(self, scenario_cls):
        """base * (sum over OBSERVED outcomes of P(o|s) * inverse-share(o)) ** T

        ⚠ An earlier version summed over ALL labels with a Laplace prior. Labels
        nobody had ever produced (rollover, minor_contact) then had count 0, so
        their 1/(1+0) term dominated the sum -- *identically for every scenario*,
        because their probability was pure prior. That constant swamped the real
        signal and flattened the weight ratio from ~3.5 to ~1.25, i.e. the bias
        did almost nothing. Restricting the sum to outcomes that have actually
        been observed somewhere is what makes the mechanism work.

        Never-observed outcomes are still reachable: a scenario with less than
        `min_history` clips keeps its static weight, which is the exploration term.
        """
        name = scenario_cls.name
        base = float(getattr(scenario_cls, "weight", 1.0))
        if not self.enabled:
            return base
        hist = self.by_scenario.get(name)
        seen = [l for l in LABELS if self.totals.get(l, 0) > 0]
        n_clips = sum(hist.values()) if hist else 0
        if not hist or not seen or n_clips < self.min_history:
            return base

        total = float(sum(self.totals.values())) or 1.0
        n = n_clips + self.prior * len(seen)
        acc = 0.0
        for label in seen:
            p = (hist.get(label, 0) + self.prior) / n
            share = self.totals[label] / total
            acc += p / share          # inverse-share: rare outcomes are worth more
        return base * (acc ** self.temperature)

    def weights(self, scenario_classes):
        return [max(1e-9, self.score(c)) for c in scenario_classes]

    def report(self):
        yields = {}
        for name, hist in self.by_scenario.items():
            n = sum(hist.values()) or 1
            yields[name] = {
                "clips": sum(hist.values()),
                "event_rate": round(1.0 - hist.get(NO_EVENT, 0) / n, 3),
                "top": max(hist, key=lambda k: hist[k]) if hist else None,
            }
        total = sum(self.totals.values()) or 1
        return {
            "outcome_totals": dict(self.totals),
            "outcome_share": {k: round(v / total, 3) for k, v in self.totals.items()},
            "entropy_bits": round(_entropy(self.totals), 3),
            "max_bits": round(math.log2(len(LABELS)), 3),
            "per_scenario": yields,
        }


def _entropy(counts):
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    h = 0.0
    for v in counts.values():
        if v > 0:
            p = v / total
            h -= p * math.log2(p)
    return h
