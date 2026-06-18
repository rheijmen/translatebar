#!/usr/bin/env python3
"""Headless test: two clients on the relay must forward captions to each other."""
import asyncio
import json
import os

import websockets

# Override with the public tunnel URL to verify wss-through-cloudflared, e.g.
#   RELAY_URI=wss://xxxx.trycloudflare.com .venv/bin/python test_relay.py
URI = os.environ.get("RELAY_URI", "ws://localhost:8765")


async def drain(ws, sink, timeout=1.0):
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            sink.append(json.loads(raw))
    except (asyncio.TimeoutError, Exception):
        pass


async def main():
    a = await websockets.connect(URI)
    b = await websockets.connect(URI)
    await a.send(json.dumps({"type": "join", "room": "test"}))
    await b.send(json.dumps({"type": "join", "room": "test"}))
    await asyncio.sleep(0.3)

    await a.send(json.dumps({"type": "caption", "text": "hello 你好", "final": True}))
    await b.send(json.dumps({"type": "caption", "text": "reply 回复", "final": True}))
    await asyncio.sleep(0.3)

    got_a, got_b = [], []
    await drain(a, got_a)
    await drain(b, got_b)

    a_caps = [m["text"] for m in got_a if m.get("type") == "caption"]
    b_caps = [m["text"] for m in got_b if m.get("type") == "caption"]
    peers_seen = max([m.get("peers", 0) for m in got_a + got_b
                      if m.get("type") == "joined"] or [0])
    print("B received from A:", b_caps)
    print("A received from B:", a_caps)
    print("max peers reported:", peers_seen)
    ok = "hello 你好" in b_caps and "reply 回复" in a_caps and peers_seen >= 2
    print("RESULT:", "RELAY OK" if ok else "FAIL")
    await a.close()
    await b.close()


if __name__ == "__main__":
    asyncio.run(main())
