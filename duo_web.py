#!/usr/bin/env python3
"""TranslateBar — duo mode with the web UI (pywebview).

Front-end = bar.html: caption text on the left (peer big = what the other says
in your language, mine small = your own check), the glow icon panel on the
right, a live status readout, and an in-app Settings panel. Backend = the engine
(your mic -> Gemini -> the peer's language) + the WebSocket relay client, both
managed by a Workers controller so settings can be applied live.

Config lives in config.json (prefs) + .env (the Gemini key) — see appconfig.py.
No CLI is required: double-click the launcher; first run opens Settings.

  .venv/bin/python relay.py                 # host side (or use host.py)
  .venv/bin/python duo_web.py               # reads config.json / .env
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import queue
import secrets
import subprocess
import threading

import webview

import appconfig
import hosting               # in-app "Start hosting" (relay + cloudflared tunnel)
import translatebar as tb    # reuse engine + .env loader

BAR_HTML = pathlib.Path(__file__).with_name("bar.html").read_text(encoding="utf-8")

# Languages offered in the Settings dropdown (code -> human label).
LANG_OPTIONS = [
    ("en", "English"),
    ("zh-Hans", "中文 (Simplified)"),
    ("zh-Hant", "中文 (Traditional)"),
    ("nl", "Nederlands"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("ja", "日本語"),
    ("ko", "한국어"),
]
LANG_LABELS = dict(LANG_OPTIONS)


def _drain(q: queue.Queue) -> None:
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


class Workers:
    """Owns the engine + relay threads for one config, restartable on save.

    A restart fully stops the old threads (join, confirm dead) BEFORE starting
    new ones, so the mic's PortAudio stream is released first — never two live
    streams at once (the old segfault path). The clean stop relies on
    DirectionWorker._stopper cancelling its TaskGroup on stop_event.
    """

    def __init__(self, ui_q, out_q, pause_event, state):
        self.ui_q, self.out_q = ui_q, out_q
        self.pause_event, self.state = pause_event, state
        self.stop_event: threading.Event | None = None
        self.engine_t: threading.Thread | None = None
        self.relay_t: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.stop_event is not None

    def _emit(self, kind, dkey, field_, text, finished):
        if kind == "update":
            if field_ == "orig":
                self.ui_q.put(("mine", text))
            elif field_ == "trans":
                self.out_q.put({"type": "caption", "text": text, "final": bool(finished)})

    def _relay_loop(self, relay_url, room, stop_event):
        async def _run():
            import websockets
            backoff = 1.0
            while not stop_event.is_set():
                try:
                    async with websockets.connect(relay_url) as ws:
                        await ws.send(json.dumps({"type": "join", "room": room}))
                        backoff = 1.0

                        async def sender():
                            while not stop_event.is_set():
                                try:
                                    msg = await asyncio.to_thread(self.out_q.get, True, 0.2)
                                except queue.Empty:
                                    continue
                                await ws.send(json.dumps(msg))

                        async def receiver():
                            async for raw in ws:
                                try:
                                    msg = json.loads(raw)
                                except Exception:
                                    continue
                                if msg.get("type") == "joined":
                                    self.state["peers"] = msg.get("peers", 0)
                                    self.ui_q.put(("conn", None))
                                elif msg.get("type") == "caption":
                                    self.ui_q.put(("peer", (msg.get("text", ""),
                                                            bool(msg.get("final")))))

                        await asyncio.gather(sender(), receiver())
                except Exception:
                    if stop_event.is_set():
                        break
                    self.state["peers"] = 0
                    self.ui_q.put(("conn", None))
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 10.0)
        asyncio.run(_run())

    def start(self, cfg, room_key):
        self.stop_event = threading.Event()
        self.state["peers"] = 0
        self.pause_event.clear()
        se = self.stop_event
        direction = tb.Direction(
            key="OUT", title=cfg["name"], target_language_code=cfg["target"],
            color="#79c0ff", source="mic", device_index=cfg.get("mic_index"),
        )
        self.engine_t = threading.Thread(
            target=tb.run_engine, args=([direction], self._emit, se, self.pause_event),
            daemon=True)
        self.engine_t.start()
        self.relay_t = threading.Thread(
            target=self._relay_loop, args=(cfg["relay"], room_key, se), daemon=True)
        self.relay_t.start()

    def stop(self, timeout=5.0) -> bool:
        """Stop and JOIN both threads. Returns False if a thread didn't die —
        the caller must then NOT start a new engine (would risk two mic streams)."""
        if self.stop_event is None:
            return True
        self.stop_event.set()
        ok = True
        for t in (self.engine_t, self.relay_t):
            if t is not None:
                t.join(timeout=timeout)
                if t.is_alive():
                    ok = False
        self.engine_t = self.relay_t = None
        self.stop_event = None
        _drain(self.out_q)          # drop captions queued by the dying workers
        return ok

    def restart(self, cfg, room_key) -> bool:
        if self.running and not self.stop():
            return False            # refuse to layer a second engine on a live one
        self.start(cfg, room_key)
        return True


def _label_payload(cfg):
    return {"name": cfg["name"], "peer": cfg["peer_name"],
            "target_label": LANG_LABELS.get(cfg["target"], cfg["target"]),
            "room": cfg["room"]}


def run_app(cfg, slot=0):
    ui_q: queue.Queue = queue.Queue()
    out_q: queue.Queue = queue.Queue()
    pause_event = threading.Event()
    ready_event = threading.Event()
    state = {"peers": 0}
    workers = Workers(ui_q, out_q, pause_event, state)
    host_holder = {"h": None}        # hosting.Host while this side hosts the relay

    window: webview.Window  # assigned below; closures use it after creation

    def layout_bar():
        try:
            scr = webview.screens[0]
            w, h = int(scr.width * 0.96), 200
            window.resize(w, h)
            window.move((scr.width - w) // 2, scr.height - h - 48 - slot * (h + 10))
        except Exception:
            pass

    def layout_card():
        try:
            scr = webview.screens[0]
            w, h = 460, 520
            window.resize(w, h)
            window.move((scr.width - w) // 2, (scr.height - h) // 2)
        except Exception:
            pass

    class Api:
        def ready(self):
            ready_event.set()

        def toggle_pause(self):
            if pause_event.is_set():
                pause_event.clear()
                return False
            pause_event.set()
            return True

        def quit(self):
            if workers.stop_event is not None:
                workers.stop_event.set()      # best-effort mic release
            if host_holder["h"] is not None:
                try:
                    host_holder["h"].stop()   # tear down relay + tunnel we started
                except Exception:
                    pass
            os._exit(0)                        # reliable on macOS (destroy() hangs)

        # --- wizard: hosting + clipboard --------------------------------------
        def start_hosting(self):
            try:
                if host_holder["h"] is None:
                    h = hosting.Host()
                    url = h.start()
                    if not url:
                        return {"ok": False, "error":
                                "Couldn't open the public tunnel — check your network."}
                    host_holder["h"] = h
                room = cfg["room"] or secrets.token_hex(4)   # generate once, reuse
                cfg["relay"] = "ws://localhost:8765"         # our own bar connects locally
                cfg["room"] = room
                link = f"{host_holder['h'].url}#{room}"       # relay + room in one link
                return {"ok": True, "url": host_holder["h"].url, "room": room, "link": link}
            except Exception as e:
                return {"ok": False, "error": repr(e)}

        def copy(self, text):
            try:
                subprocess.run(["pbcopy"], input=(text or "").encode(), timeout=3)
                return True
            except Exception:
                return False

        # --- settings ---------------------------------------------------------
        def get_settings(self):
            return {
                "name": cfg["name"], "peer_name": cfg["peer_name"],
                "target": cfg["target"], "mic_index": cfg["mic_index"],
                "relay": cfg["relay"], "room": cfg["room"],
                "has_key": appconfig.has_key(),
                "langs": [{"code": c, "label": l} for c, l in LANG_OPTIONS],
                "mics": tb.list_input_devices(),
            }

        def enter_settings(self):
            layout_card()
            return True

        def exit_settings(self):
            layout_bar()
            return True

        def open_key_help(self):
            import webbrowser
            webbrowser.open("https://aistudio.google.com/apikey")
            return True

        def save_settings(self, payload):
            try:
                cfg["name"] = (payload.get("name") or "").strip() or "You"
                cfg["peer_name"] = (payload.get("peer_name") or "").strip() or "Team"
                cfg["target"] = payload.get("target") or cfg["target"]
                mi = payload.get("mic_index")
                cfg["mic_index"] = int(mi) if str(mi).lstrip("-").isdigit() else None
                cfg["relay"] = (payload.get("relay") or "").strip()
                cfg["room"] = (payload.get("room") or "").strip()
                key = (payload.get("key") or "").strip()
                if key:
                    appconfig.set_key(key)
                appconfig.save(cfg)

                if not appconfig.has_key():
                    return {"ok": False, "error": "Enter your Gemini API key."}
                if not cfg["relay"]:
                    return {"ok": False, "error": "No connection yet — start hosting or paste an invite."}
                if not cfg["room"]:
                    return {"ok": False, "error": "Missing room — start hosting or paste an invite."}

                if not workers.restart(cfg, cfg["room"]):
                    return {"ok": False, "error":
                            "Couldn't restart cleanly — please close and reopen the app."}
                ui_q.put(("relabel", _label_payload(cfg)))
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": repr(e)}

    window = webview.create_window(
        "TranslateBar", html=BAR_HTML, js_api=Api(),
        frameless=True, on_top=True, transparent=True, easy_drag=True,
        width=1200, height=200,
    )

    def pump():
        layout_bar()
        ready_event.wait(timeout=5)

        def js(call):
            try:
                window.evaluate_js(call)
            except Exception:
                pass

        p = _label_payload(cfg)
        js(f"setLabels({json.dumps(p['name'])},{json.dumps(p['peer'])},"
           f"{json.dumps(p['target_label'])},{json.dumps(p['room'])})")

        # Always run the wizard at launch (it sets up this meeting's connection);
        # its Start button persists the config + starts the workers.
        js("startWizard()")

        last_conn = None
        while True:                   # app lifetime; quit() does os._exit
            try:
                ch, payload = ui_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if pause_event.is_set() and ch in ("peer", "mine"):
                continue              # paused: freeze display, drop backlog
            if ch == "peer":
                txt, fin = payload
                js(f"pushPeer({json.dumps(txt)}, {str(fin).lower()})")
            elif ch == "mine":
                js(f"pushMine({json.dumps(payload[-400:])})")
            elif ch == "conn":
                connected = state["peers"] >= 2
                if connected != last_conn:
                    last_conn = connected
                    js(f"setLink({str(connected).lower()})")
            elif ch == "relabel":
                pl = payload
                js(f"setLabels({json.dumps(pl['name'])},{json.dumps(pl['peer'])},"
                   f"{json.dumps(pl['target_label'])},{json.dumps(pl['room'])})")
                last_conn = None      # re-push link state after a restart

    webview.start(pump)


def main():
    ap = argparse.ArgumentParser(description="TranslateBar duo (web UI)")
    ap.add_argument("--room", default=None, help="Meet code/URL or literal room (overrides config)")
    ap.add_argument("--target", default=None, help="language to translate YOUR mic into")
    ap.add_argument("--relay", default=None, help="relay ws:// or wss:// URL (overrides config)")
    ap.add_argument("--mic-device", default=None, help="mic index or name substring (overrides config)")
    ap.add_argument("--name", default=None)
    ap.add_argument("--peer-name", default=None)
    ap.add_argument("--slot", type=int, default=0, help="stack position for local testing")
    args = ap.parse_args()

    cfg = appconfig.load()            # CLI args override persisted config (session-only)
    if args.name:
        cfg["name"] = args.name
    if args.peer_name:
        cfg["peer_name"] = args.peer_name
    if args.target:
        cfg["target"] = args.target
    if args.relay:
        cfg["relay"] = args.relay
    if args.room is not None:
        cfg["room"] = args.room
    if args.mic_device:
        cfg["mic_index"] = tb.resolve_device(args.mic_device)

    run_app(cfg, slot=args.slot)


if __name__ == "__main__":
    main()
