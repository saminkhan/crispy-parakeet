#!/usr/bin/env python3
"""Capture synthetic driving clips from GTA V. Edit one json file, run one command.

    python3 run_capture.py --config capture.json
    python3 run_capture.py --config capture.json --target 200 --hours 8
    python3 run_capture.py --config capture.json --dry-run     # check, launch nothing
    python3 run_capture.py --config capture.json --status      # what is on disk
    python3 run_capture.py --config capture.json --variations counterfactual
    python3 run_capture.py --config capture.json --variations off

The run is resumable: stop it with Ctrl-C, start it again against the same
output_dir, and it continues from what is already there rather than starting over.

--variations overrides the config file's `variations`: a preset name captures every
proposed location N times with different ego/actor behaviour (one scene, N clips);
"off" captures one clip per location. --dry-run prints the resulting plan.
"""

import argparse
import os
import sys

# ⚠ Absolute, and inserted before anything else is imported, so the command works
# from any working directory -- including a service or a cron job whose cwd is /.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from capture import runner
from capture.settings import CaptureSettings


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True,
                    help="path to the capture settings json")
    ap.add_argument("--target", type=int, default=None,
                    help="override target_clips (kept clips wanted; 0 = no limit)")
    ap.add_argument("--hours", type=float, default=None,
                    help="override max_hours (wall-clock budget; 0 = no limit)")
    ap.add_argument("--variations", default=None, metavar="PRESET|off",
                    help="override the config's variations: a preset name (e.g. "
                         "\"counterfactual\": each scene captured 4 times, ego and "
                         "actors sane/chaotic in every combination) or \"off\" for "
                         "one clip per location")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and print what would happen, then exit")
    ap.add_argument("--status", action="store_true",
                    help="print the manifest summary for the output dir and exit")
    args = ap.parse_args(argv)

    try:
        settings = CaptureSettings.load(args.config)
    except Exception as exc:
        # The loader's own message names the offending key or field; a traceback
        # on top of it just buries the one line the user needs.
        sys.stderr.write("[capture] cannot load %s:\n    %s\n" % (args.config, exc))
        return 2

    if args.target is not None:
        settings.target_clips = args.target
    if args.hours is not None:
        settings.max_hours = args.hours
    if args.variations is not None:
        err = runner.override_variations(settings, args.variations)
        if err:
            sys.stderr.write("[capture] --variations: %s\n" % err)
            return 2

    if args.status:
        return runner.status(settings)
    if args.dry_run:
        return runner.dry_run(settings)
    return runner.run(settings)


if __name__ == "__main__":
    sys.exit(main())
