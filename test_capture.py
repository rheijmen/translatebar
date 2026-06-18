#!/usr/bin/env python3
"""Headless smoke test for the callback-mode mic capture (no network).

Runs _produce_mic against the default input device for ~3s, draining frames,
to confirm the new capture path delivers audio and does not crash.
"""
import asyncio
import threading
import time

import translatebar as tb


async def main():
    d = tb.Direction(key="OUT", title="test", target_language_code="zh-Hans",
                     color="", source="mic", device_index=None)
    stop = threading.Event()
    worker = tb.DirectionWorker(client=None, direction=d, emit=lambda *a: None,
                                stop_event=stop, pause_event=None)
    worker._loop = asyncio.get_running_loop()
    worker._audio_q = asyncio.Queue(maxsize=200)

    frames = {"n": 0, "bytes": 0}

    async def consume():
        while not stop.is_set():
            try:
                data = await asyncio.wait_for(worker._audio_q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            frames["n"] += 1
            frames["bytes"] += len(data)

    async def timer():
        await asyncio.sleep(3.0)
        stop.set()

    try:
        await asyncio.gather(worker._produce_mic(), consume(), timer())
    except Exception as e:
        print(f"ERROR: {e!r}")
        return
    rate_ok = frames["n"] > 0
    print(f"frames={frames['n']}  bytes={frames['bytes']}  "
          f"(expected 16kHz mono -> ~{frames['bytes']//2} samples / ~"
          f"{frames['bytes']/2/16000:.1f}s)")
    print("RESULT:", "CAPTURE OK" if rate_ok else "NO FRAMES")


if __name__ == "__main__":
    asyncio.run(main())
