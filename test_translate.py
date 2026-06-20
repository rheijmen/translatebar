#!/usr/bin/env python3
"""Live engine check: capture the mic for a few seconds, run the chunked
Flash-Lite translation, and print each translated utterance. Verifies the real
model id + that it translates from audio + the quality/VAD. Needs GEMINI_API_KEY
in .env.

  .venv/bin/python test_translate.py --target nl       # speak English -> see Dutch
  .venv/bin/python test_translate.py --target zh-Hans  # -> Chinese (the real use)
"""
import argparse
import os
import threading
import time

import translatebar as tb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="nl")
    ap.add_argument("--engine", default="chunked", help="chunked | live")
    ap.add_argument("--model", default=None, help="model for the active engine")
    ap.add_argument("--seconds", type=int, default=14)
    a = ap.parse_args()
    if not (os.environ.get("GEMINI_API_KEY") or "").strip():
        raise SystemExit("No GEMINI_API_KEY — put it in .env first (open -e .env).")

    stop, pause = threading.Event(), threading.Event()

    def emit(kind, dkey, field_, text, finished):
        if kind == "update" and field_ == "trans" and finished:
            print(f"  → {text}", flush=True)
        elif kind == "error":
            print(f"  [error] {text}", flush=True)
        elif kind == "status":
            print(f"  [{text}]", flush=True)

    d = tb.Direction(key="OUT", title="test", target_language_code=a.target,
                     color="#fff", source="mic")
    t = threading.Thread(target=tb.run_engine,
                         args=([d], emit, stop, pause, a.engine, a.model), daemon=True)
    print(f"Speak now for ~{a.seconds}s.  engine={a.engine}  target={a.target}  "
          f"model={a.model or '(default)'}", flush=True)
    t.start()
    time.sleep(a.seconds)
    stop.set()
    t.join(timeout=6)
    print("done." if not t.is_alive() else "warn: engine thread still alive", flush=True)


if __name__ == "__main__":
    main()
