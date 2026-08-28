"""Start the WVI server.

    python run.py

Binds to 0.0.0.0, which means "listen on every network interface this machine
has" - as opposed to 127.0.0.1, which would only accept connections from the
machine itself. That distinction is REQ-F-08: other computers must be able to
reach it.
"""
import os
import socket
import sys

import uvicorn

# Make `python /some/path/to/run.py` work from any working directory. Uvicorn
# imports the app by the string "app.main:app", which needs this file's folder
# on the import path - and on a Pi you will absolutely start this from a
# systemd unit or an ssh session that is not sitting in the project folder.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

HOST = os.getenv("WVI_HOST", "0.0.0.0")
PORT = int(os.getenv("WVI_PORT", "8000"))


def lan_ip() -> str:
    """Best guess at the address other machines should type in."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packet is actually sent
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    print("=" * 62)
    print("  UAVPayloadTAQ-26  |  Web Visualisation (WVI)")
    print(f"  This machine : http://127.0.0.1:{PORT}")
    print(f"  Other devices: http://{lan_ip()}:{PORT}")
    print(f"  API docs     : http://127.0.0.1:{PORT}/docs")
    print("=" * 62)
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
