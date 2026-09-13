"""Turn-key GTA V clip capture: one JSON file, one command.

    python3 run_capture.py --config capture.json

The package is a supervisor around the existing `VPilot/longtail` generator, not
a replacement for it. `CaptureSettings` is the small user-facing surface;
everything else here keeps the game alive, finalises finished clips off the
capture path, and maintains the record of what was produced.

Only `CaptureSettings` is re-exported. The other modules are imported directly
(`from capture.runner import run`) so that importing this package does not drag
in the game link or the encoder for a caller that only wants to read a config.
"""

from .settings import CaptureSettings, GRAPHICS_PRESETS

__all__ = ["CaptureSettings", "GRAPHICS_PRESETS"]
