"""Long-tail clip generation for GTA V, built on David0tt/DeepGTAV.

Run from the VPilot directory:
    python -m longtail.run_generator --out <dir>
"""

import os as _os
import sys as _sys

# Modules here import each other flatly (``import posemath``) so that they can be
# run as scripts from this directory as well as imported as ``longtail.x``.
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
