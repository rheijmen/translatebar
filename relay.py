#!/usr/bin/env python3
"""Minimal room-based WebSocket relay for TranslateBar duo mode.

Each peer sends {"type":"join","room":"<key>"} once, then small JSON caption
messages. The relay forwards every message to the *other* members of the same
room. It never sees audio — only tiny text payloads — so bandwidth is trivial.

Run locally:   .venv/bin/python relay.py        # ws://0.0.0.0:8765
Env overrides: RELAY_HOST, RELAY_PORT
"""
import asyncio
import json
import os

import websockets

ROOMS: dict[str, set] = {}  # room key -> set of connected sockets


def _broadcast_count(room: str) -> int:
    return len(ROOMS.get(room, ()))


async def handler(ws):
    room = None
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            if msg.get("type") == "join":
                room = msg.get("room") or "default"
                ROOMS.setdefault(room, set()).add(ws)
                count = _broadcast_count(room)
                # tell everyone in the room how many peers are present now
                for peer in list(ROOMS[room]):
                    try:
                        await peer.send(json.dumps({"type": "joined",
                                                    "room": room, "peers": count}))
                    except Exception:
                        pass
                print(f"join room={room!r} peers={count}")
                continue

            if room is None:
                continue  # must join before sending

            # forward to the other members of the room
            for peer in list(ROOMS.get(room, ())):
                if peer is ws:
                    continue
                try:
                    await peer.send(raw)
                except Exception:
                    ROOMS.get(room, set()).discard(peer)
    finally:
        if room and ws in ROOMS.get(room, set()):
            ROOMS[room].discard(ws)
            count = _broadcast_count(room)
            for peer in list(ROOMS.get(room, ())):
                try:
                    await peer.send(json.dumps({"type": "joined",
                                                "room": room, "peers": count}))
                except Exception:
                    pass
            if not ROOMS[room]:
                ROOMS.pop(room, None)
            print(f"leave room={room!r} peers={count}")


async def main():
    host = os.environ.get("RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("RELAY_PORT", "8765"))
    print(f"relay listening on ws://{host}:{port}")
    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
