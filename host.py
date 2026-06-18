#!/usr/bin/env python3
"""Host the relay for a cross-border meeting.

Starts relay.py + a public cloudflared tunnel and prints the wss:// URL to
share with the other side. Run it on the machine that hosts the relay (yours):

    .venv/bin/python host.py

Then both sides run the bar against the printed URL, e.g.:
    you : .venv/bin/python duo_web.py --relay <wss-url> --room <code> --target zh-Hans
    peer: python duo_web.py --relay <wss-url> --room <code> --target en

Ctrl-C stops the tunnel and the relay.
"""
from __future__ import annotations

import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
PY = sys.executable
PORT = os.environ.get("RELAY_PORT", "8765")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

_procs: list[subprocess.Popen] = []


def _port_busy(port: str) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def _spawn(cmd: list[str]) -> subprocess.Popen:
    p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    _procs.append(p)
    return p


def _pump(p: subprocess.Popen, on_line=None) -> None:
    for line in p.stdout:                       # type: ignore[union-attr]
        if on_line:
            on_line(line)


def _cleanup() -> None:
    for p in _procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in _procs:
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def main() -> None:
    if not (HERE / "relay.py").exists():
        sys.exit("relay.py not found next to host.py")
    if subprocess.run(["which", "cloudflared"], capture_output=True).returncode != 0:
        sys.exit("cloudflared not installed — run: brew install cloudflared")

    if _port_busy(PORT):
        print(f"Reusing relay already listening on localhost:{PORT}")
    else:
        print(f"Starting relay on localhost:{PORT} …")
        relay = _spawn([PY, "relay.py"])
        threading.Thread(target=_pump, args=(relay,), daemon=True).start()
        time.sleep(1.0)

    print("Opening public tunnel (cloudflared) …")
    found = {"url": None}

    def watch(line: str) -> None:
        if not found["url"]:
            m = URL_RE.search(line)
            if m:
                found["url"] = m.group(0)

    tun = _spawn(["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}"])
    threading.Thread(target=_pump, args=(tun, watch), daemon=True).start()

    for _ in range(40):
        if found["url"]:
            break
        time.sleep(0.5)
    if not found["url"]:
        _cleanup()
        sys.exit("Could not obtain a tunnel URL (check your network).")

    wss = found["url"].replace("https://", "wss://")
    bar = "="*68
    print("\n" + bar)
    print("  SHARE THIS URL with the other side (live until you stop host.py):")
    print("      " + wss)
    print(bar)
    print("\n  You (English speaker) — start your bar:")
    print(f"      {PY} duo_web.py --relay {wss} --target zh-Hans")
    print("  Peer (Chinese side) — they run:")
    print(f"      python duo_web.py --relay {wss} --target en --room <meet-code>")
    print("\n  Use the SAME --room on both sides (e.g. the Meet code abc-defg-hij).")
    print("  On macOS you may omit --room to auto-detect the open Google Meet tab.")
    print("  Ctrl-C here stops the tunnel + relay.\n")

    try:
        while True:
            time.sleep(1)
            if any(p.poll() is not None for p in _procs):
                print("A subprocess exited — shutting down.")
                break
    except KeyboardInterrupt:
        print("\nStopping …")
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
