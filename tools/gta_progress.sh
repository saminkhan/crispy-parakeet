#!/usr/bin/env bash
# Does the plugin actually PRODUCE FRAMES? Exit 0 if yes, 1 if not.
#
# ★ gta_health.sh deliberately tests liveness only -- process alive, socket bound
# -- and its own header warns that a deadlocked script thread keeps it green while
# no frames are produced at all. That warning is exactly the failure this exists to
# catch: an overnight run reused a "healthy" game seven times that was in fact hung,
# and every one of those chunks produced zero clips, the client failing with
# ZMQ Again('Resource temporarily unavailable').
#
# ⚠ Safe ONLY between chunks. ZMQ PAIR allows a single peer, so opening the socket
# while a capture client holds it will fight with it.
set -u
HOST="${DEEPGTAV_HOST:-172.28.32.1}"
PORT="${DEEPGTAV_PORT:-8000}"
TIMEOUT="${1:-20}"
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$R/VPilot" || exit 1
timeout $((TIMEOUT + 10)) python3 - "$HOST" "$PORT" "$TIMEOUT" <<'PY'
import sys, time
sys.path.insert(0, ".")
from deepgtav.client import Client
from deepgtav.messages import Start, Config, Dataset, Scenario as GtaScenario, StartRecording, Stop

host, port, budget = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
try:
    c = Client(ip=host, port=port, recv_timeout_ms=int(budget * 1000))
except Exception as e:
    print("no-connect %r" % (e,)); sys.exit(1)
try:
    c.sendMessage(Start(dataset=Dataset(rate=10, frame=[320, 180])))
    c.sendMessage(StartRecording())
    t0 = time.time()
    msg = c.recvMessage()
    dt = time.time() - t0
    if msg is None or msg.get("frame") is None:
        print("no-frame after %.1fs" % dt); sys.exit(1)
    n = len(msg["frame"])
    print("producing frames (%d bytes in %.1fs)" % (n, dt))
    sys.exit(0 if n > 0 else 1)
except Exception as e:
    print("no-frame %r" % (e,)); sys.exit(1)
finally:
    try:
        c.sendMessage(Stop()); c.close()
    except Exception:
        pass
PY
