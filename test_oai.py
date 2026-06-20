#!/usr/bin/env python3
"""Probe: OpenAI gpt-realtime-translate. Streams your mic, prints the Chinese
translated transcript deltas (and whether they're incremental or cumulative).
Run it yourself so you see the prompt and control the timing:

  .venv/bin/python test_oai.py            # English -> Chinese (zh)
  .venv/bin/python test_oai.py en 12      # target lang + seconds
"""
import asyncio
import base64
import json
import os
import sys
import threading

import websockets

import translatebar as tb

LANG = sys.argv[1] if len(sys.argv) > 1 else "zh"
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 15
URL = "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate"
stop, pause = threading.Event(), threading.Event()


async def go():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("No OPENAI_API_KEY in env."); return
    async with websockets.connect(URL, additional_headers={"Authorization": f"Bearer {key}"},
                                   max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.update",
                                  "session": {"audio": {"output": {"language": LANG}}}}))
        print(f"\n>>> SPEAK ENGLISH NOW for ~{SECONDS}s (target={LANG}) <<<\n", flush=True)

        async def sender():
            async for frame in tb._mic_frames(stop, pause, None, out_rate=24000):
                await ws.send(json.dumps({"type": "session.input_audio_buffer.append",
                                          "audio": base64.b64encode(frame).decode()}))

        async def receiver():
            async for raw in ws:
                m = json.loads(raw); t = m.get("type")
                if t == "session.output_transcript.delta":
                    print("  ZH:", repr(m.get("delta")), flush=True)
                elif t == "session.input_transcript.delta":
                    print("  EN:", repr(m.get("delta")), flush=True)
                elif t == "error":
                    print("  ERROR:", m.get("error"), flush=True)

        try:
            await asyncio.wait_for(asyncio.gather(sender(), receiver()), timeout=SECONDS)
        except asyncio.TimeoutError:
            print("\n(done)", flush=True)


asyncio.run(go())
