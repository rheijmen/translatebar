#!/usr/bin/env python3
"""Persistent settings for the duo web bar.

Preferences (name, peer, target, mic, relay, room) live in config.json next to
the app. The Gemini API key lives in .env — the gitignored secret file the
engine already reads via translatebar._load_dotenv. Writing the key also updates
os.environ directly, because _load_dotenv only sets vars that aren't already in
the environment, so a key *change* needs the live override too.
"""
from __future__ import annotations

import json
import os
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
ENV_PATH = HERE / ".env"
KEY_VAR = "GEMINI_API_KEY"

DEFAULTS = {
    "name": "You",
    "peer_name": "Team",
    "target": "zh-Hans",
    "mic_index": None,            # None = system default input device
    "relay": "ws://localhost:8765",
    "room": "",                   # "" => auto-detect the open Meet tab at launch
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass                       # missing/corrupt -> fall back to defaults
    return {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}


def save(cfg: dict) -> None:
    keep = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    CONFIG_PATH.write_text(json.dumps(keep, indent=2) + "\n", encoding="utf-8")


def get_key() -> str:
    return os.environ.get(KEY_VAR, "") or ""


def has_key() -> bool:
    return bool(get_key().strip())


def set_key(key: str) -> None:
    key = (key or "").strip()
    os.environ[KEY_VAR] = key       # live override (see module docstring)
    lines, found = [], False
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith(f"{KEY_VAR}=") or stripped.startswith(f"{KEY_VAR} ="):
                lines.append(f"{KEY_VAR}={key}")
                found = True
            else:
                lines.append(line)
    except FileNotFoundError:
        pass
    if not found:
        lines.append(f"{KEY_VAR}={key}")
    # owner-only permissions — this file holds the API key
    data = "\n".join(lines) + "\n"
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    try:
        os.chmod(ENV_PATH, 0o600)        # tighten if it pre-existed with broader perms
    except OSError:
        pass
