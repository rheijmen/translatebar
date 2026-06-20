#!/usr/bin/env python3
"""Headless: restart the (chunked) engine + relay several times and prove each
stop() fully joins — so a restart never layers a second mic stream on a live one
(the old PortAudio segfault). Uses a DUMMY key so the client builds and the
mic-capture/VAD task runs; the actual translate calls fail and are swallowed —
we're testing teardown, not translation. No GUI, no network for the relay."""
import os
import queue
import threading
import time

# force a non-empty key so make_client() builds (the real .env may be empty);
# this only touches THIS process's env, never the .env file.
os.environ["GEMINI_API_KEY"] = "dummy-test-key-not-real"

import appconfig
import duo_web


def main():
    cfg = appconfig.load()
    cfg["relay"] = "ws://127.0.0.1:9"          # nothing there -> relay just backs off
    cfg["engine"] = "chunked"
    ui_q, out_q = queue.Queue(), queue.Queue()
    pause = threading.Event()
    state = {"peers": 0}
    w = duo_web.Workers(ui_q, out_q, pause, state)

    for i in range(1, 4):
        w.start(cfg, "restarttest")
        time.sleep(3.0)
        alive = w.engine_t.is_alive() and w.relay_t.is_alive()
        ok = w.stop(timeout=6)
        print(f"cycle {i}: threads_were_alive={alive} stop_clean={ok} still_running={w.running}")
        if not ok:
            print("RESULT: FAIL (a worker thread did not stop within timeout)")
            return
        time.sleep(0.5)
    print("RESULT: RESTART CYCLE OK")


if __name__ == "__main__":
    main()
