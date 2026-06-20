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
import sys

HERE = pathlib.Path(__file__).resolve().parent
# In a packaged .app you can't write next to the program (read-only bundle), so
# prefs + key live in a user data dir. In dev they stay next to the script (keeps
# the existing .env/config.json working).
if getattr(sys, "frozen", False):
    _BASE = pathlib.Path.home() / "Library" / "Application Support" / "LiveTranslateBar"
    try:
        _BASE.mkdir(parents=True, exist_ok=True)
    except OSError:
        _BASE = HERE
else:
    _BASE = HERE
CONFIG_PATH = _BASE / "config.json"
ENV_PATH = _BASE / ".env"
KEY_VAR = "GEMINI_API_KEY"
OPENAI_KEY_VAR = "OPENAI_API_KEY"


def _load_env() -> None:
    """Load KEY=VALUE lines from ENV_PATH into os.environ (the packaged app can't
    rely on translatebar._load_dotenv, which looks next to the bundled script)."""
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip()
    except FileNotFoundError:
        pass


_load_env()

DEFAULTS = {
    "name": "You",
    "peer_name": "Team",
    "target": "zh-Hans",
    "mic_index": None,            # None = system default input device
    "relay": "ws://localhost:8765",
    "room": "",                   # "" => auto-detect the open Meet tab at launch
    "engine": "live",             # "live" = Gemini translate (streaming) | "openai"
    "live_model": "gemini-3.5-live-translate-preview",  # Gemini engine model
    "openai_model": "gpt-realtime-translate",           # OpenAI engine model
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


def get_key(var: str = KEY_VAR) -> str:
    return os.environ.get(var, "") or ""


def has_key(var: str = KEY_VAR) -> bool:
    return bool(get_key(var).strip())


def set_key(key: str, var: str = KEY_VAR) -> None:
    key = (key or "").strip()
    os.environ[var] = key            # live override (see module docstring)
    lines, found = [], False
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith(f"{var}=") or stripped.startswith(f"{var} ="):
                lines.append(f"{var}={key}")
                found = True
            else:
                lines.append(line)
    except FileNotFoundError:
        pass
    if not found:
        lines.append(f"{var}={key}")
    # owner-only permissions — this file holds API keys
    data = "\n".join(lines) + "\n"
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
    try:
        os.chmod(ENV_PATH, 0o600)        # tighten if it pre-existed with broader perms
    except OSError:
        pass
