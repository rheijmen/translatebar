#!/usr/bin/env python3
"""Reusable host side: start relay.py (if not already running) + a public
cloudflared tunnel, and hand back the wss:// URL to share. Used by the in-app
wizard (duo_web) and by host.py."""
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
DEFAULT_PORT = os.environ.get("RELAY_PORT", "8765")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def port_busy(port: str) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


class Host:
    """Starts (or reuses) the relay + a cloudflared tunnel. After start(),
    `.url` is the public wss:// address to share. Call stop() to tear down."""

    def __init__(self, port: str = DEFAULT_PORT):
        self.port = port
        self.url: str | None = None
        self._procs: list[subprocess.Popen] = []

    def _spawn(self, cmd):
        p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
        self._procs.append(p)
        return p

    def start(self, timeout: float = 25.0) -> str | None:
        if subprocess.run(["which", "cloudflared"], capture_output=True).returncode != 0:
            raise RuntimeError("cloudflared is not installed (brew install cloudflared)")
        if not port_busy(self.port):
            self._spawn([PY, str(HERE / "relay.py")])
            time.sleep(1.0)
        found = {"url": None}

        def watch(p):
            for line in p.stdout:                       # type: ignore[union-attr]
                if not found["url"]:
                    m = URL_RE.search(line)
                    if m:
                        found["url"] = m.group(0)

        tun = self._spawn(["cloudflared", "tunnel", "--url", f"http://localhost:{self.port}"])
        threading.Thread(target=watch, args=(tun,), daemon=True).start()

        waited = 0.0
        while waited < timeout and not found["url"]:
            time.sleep(0.25)
            waited += 0.25
        if found["url"]:
            self.url = found["url"].replace("https://", "wss://")
        return self.url

    def stop(self):
        for p in self._procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self._procs = []
        self.url = None
