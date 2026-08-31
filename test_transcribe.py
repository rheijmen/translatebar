#!/usr/bin/env python3
"""Offline check of the transcribe engine's segmenting: partial transcripts
emit 'orig' updates, the finished flag flushes exactly one clean segment onto
the translation queue, the quiet-gap finalizer catches segments that never get
the flag, and empty/whitespace segments are dropped. No network, no mic, no
API key.

  .venv/bin/python test_transcribe.py
"""
import asyncio
import threading

import translatebar as tb


def make_worker(events):
    d = tb.Direction(key="OUT", title="test", target_language_code="nl",
                     color="", source="mic")
    w = tb.TranscribeWorker(client=None, direction=d,
                            emit=lambda *a: events.append(a),
                            stop_event=threading.Event())
    w._text_q = asyncio.Queue(maxsize=20)
    return w


def main():
    failures = []

    def expect(cond, label):
        print(("ok  " if cond else "FAIL"), label)
        if not cond:
            failures.append(label)

    # partials accumulate and emit 'orig'; finished flushes one segment
    events = []
    w = make_worker(events)
    w._on_transcript("Hello ", False, now=1.0)
    w._on_transcript("world.", True, now=1.2)
    expect(w._text_q.qsize() == 1, "finished flag queues exactly one segment")
    expect(w._text_q.get_nowait() == "Hello world.", "segment text is the joined transcript")
    expect(w._seg == "", "buffer resets after flush")
    updates = [e for e in events if e[0] == "update" and e[2] == "orig"]
    expect(len(updates) == 2 and updates[0][4] is False and updates[1][4] is True,
           "partial then final 'orig' updates emitted")

    # quiet-gap finalizer: no finished flag, gap >= FINALIZE_S flushes
    events = []
    w = make_worker(events)
    w._on_transcript("Nog bezig", False, now=5.0)
    expect(not w._maybe_finalize(5.0 + w.FINALIZE_S / 2), "no flush before the quiet gap")
    expect(w._maybe_finalize(5.0 + w.FINALIZE_S), "quiet gap flushes the segment")
    expect(w._text_q.get_nowait() == "Nog bezig", "finalizer queues the pending text")
    expect(any(e[0] == "update" and e[2] == "orig" and e[4] is True for e in events),
           "finalizer emits a final 'orig' update")

    # whitespace-only transcript never reaches the translator
    events = []
    w = make_worker(events)
    w._on_transcript("   ", True, now=9.0)
    expect(w._text_q.qsize() == 0, "whitespace segment is dropped")

    print("RESULT:", "PASS" if not failures else f"{len(failures)} FAILED")
    raise SystemExit(0 if not failures else 1)


if __name__ == "__main__":
    main()
