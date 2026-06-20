#!/usr/bin/env python3
"""
TranslateBar — live two-way EN <-> ZH caption bar for Google Meet,
powered by Gemini 3.5 Live Translate (gemini-3.5-live-translate-preview).

It shows a slim, always-on-top caption bar you can pin to the bottom of your
screen. Share your *full screen* in Google Meet and both you and the Chinese
team see the live captions.

Two independent directions (each is its own Live API session — one can drop
without taking the other down):
  OUT : your microphone (English)        -> Simplified Chinese captions  [zero setup]
  IN  : the meeting's incoming audio (中文) -> English captions          [needs BlackHole]

The model only emits audio; we display the *text transcripts* the Live API
returns alongside it:
  - input_transcription  = what was heard (the original)
  - output_transcription = the translation

Quick start (macOS):
  export GEMINI_API_KEY="your-key"
  python translatebar.py --check                 # no network: verify install + config
  python translatebar.py --list-devices          # find your audio input devices
  python translatebar.py                          # mic -> Chinese only (no extra setup)
  python translatebar.py --in-device "BlackHole"  # also: Meet audio -> English
  python translatebar.py --selftest sample.wav --direction out   # end-to-end test, no GUI

See README_translatebar.md for the BlackHole / screen-share setup.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import sys
import threading
import wave
from dataclasses import dataclass, field


# ---- .env loader (no dependency) ---------------------------------------------
def _load_dotenv():
    """Load KEY=VALUE pairs from a .env file next to this script into the
    environment, without overriding variables already set in the shell."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass


_load_dotenv()


# ---- Audio / model constants -------------------------------------------------
CHANNELS = 1
SEND_SAMPLE_RATE = 16000          # Gemini Live expects 16 kHz PCM input
CHUNK = 1024
AUDIO_MIME = f"audio/pcm;rate={SEND_SAMPLE_RATE}"
MODEL = "gemini-3.5-live-translate-preview"   # "live" engine: speech -> speech
DEFAULT_CHUNK_MODEL = "gemini-3.1-flash-lite"  # "chunked" engine: audio -> text (cheap)
OPENAI_TRANSLATE_MODEL = "gpt-realtime-translate"  # "openai" engine: streaming translate
OPENAI_SR = 24000                              # OpenAI realtime wants 24 kHz PCM16

# Chinese codes accepted by the model are zh-Hans / zh-Hant (NOT zh / zh-CN).
DEFAULT_TARGET_THEM = "zh-Hans"   # OUT: your English -> their Chinese
DEFAULT_TARGET_YOU = "en"         # IN:  their Chinese -> your English


# ---- Config builder (verified against google-genai 2.8.0) --------------------
def build_config(target_language_code: str, model: str = MODEL):
    from google.genai import types
    common = dict(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )
    if "translate" in model:
        # Purpose-built translate model. echo_target_language=False: only emit the
        # TRANSLATION, never parrot the source. (echo=True made it intermittently
        # repeat the source language -> the English<->Chinese flicker.) Each side
        # speaks its own (non-target) language, so this translates cleanly; the
        # only trade-off is no caption when someone speaks the target language.
        return types.LiveConnectConfig(
            **common,
            translation_config=types.TranslationConfig(
                target_language_code=target_language_code,
                echo_target_language=False,
            ),
        )
    # General native-audio Live model (cheaper): make it interpret via a system
    # instruction. It still speaks the translation; we read the transcription.
    return types.LiveConnectConfig(
        **common,
        system_instruction=(
            f"You are a simultaneous interpreter. Translate everything you hear "
            f"into {target_language_code} and speak ONLY the translation — no "
            f"greetings, answers, or commentary. If the speech is already in "
            f"{target_language_code}, repeat it as-is."),
    )


@dataclass
class Direction:
    """One translation lane."""
    key: str                 # "IN" or "OUT"
    title: str               # shown on the bar
    target_language_code: str
    color: str               # translation text color
    source: str              # "mic", "device", or "wav"
    device_index: int | None = None
    wav_path: str | None = None


# ---- The translation engine (no GUI; usable headless) ------------------------
class _StopWorker(Exception):
    """Raised by the stopper task to break the TaskGroup cleanly on stop_event.
    Cancelling the produce/send/receive tasks lets the Live session close and
    _produce_mic's finally release the PortAudio stream — so a restarted worker
    never opens a second mic stream over a live one (the old segfault path)."""


async def _mic_frames(stop_event, pause_event, device_index, out_rate=SEND_SAMPLE_RATE):
    """Async generator of mono PCM frames from the mic at `out_rate` Hz (16 kHz
    for Gemini, 24 kHz for OpenAI). Callback-mode PortAudio (avoids the
    blocking-read segfault), resampling off the audio thread, stream closed on
    stop. Skips frames while paused but keeps draining the device so it never
    overflows. Shared by all engines."""
    import pyaudio
    import audioop  # stdlib (py3.12); downmix + resample to 16 kHz mono
    import queue as _queue
    pa = pyaudio.PyAudio()
    idx = device_index
    if idx is None:
        idx = pa.get_default_input_device_info()["index"]
    info = pa.get_device_info_by_index(idx)
    native_rate = int(info.get("defaultSampleRate", SEND_SAMPLE_RATE))
    in_ch = min(2, int(info.get("maxInputChannels", 1)) or 1)
    raw_q: "queue.Queue" = _queue.Queue(maxsize=100)

    def _cb(in_data, frame_count, time_info, status):
        try:
            raw_q.put_nowait(in_data)
        except _queue.Full:
            pass  # consumer fell behind: drop rather than stall
        return (None, pyaudio.paComplete if stop_event.is_set() else pyaudio.paContinue)

    stream = await asyncio.to_thread(
        pa.open, format=pyaudio.paInt16, channels=in_ch, rate=native_rate,
        input=True, input_device_index=idx, frames_per_buffer=CHUNK, stream_callback=_cb)
    rs_state = None
    try:
        while not stop_event.is_set():
            if not stream.is_active():
                raise OSError("input stream stopped (device change?)")
            try:
                data = await asyncio.to_thread(raw_q.get, True, 0.2)
            except _queue.Empty:
                continue
            if pause_event is not None and pause_event.is_set():
                continue  # paused: keep capturing but emit nothing
            if in_ch == 2:
                data = audioop.tomono(data, 2, 0.5, 0.5)
            if native_rate != out_rate:
                data, rs_state = audioop.ratecv(
                    data, 2, 1, native_rate, out_rate, rs_state)
            yield data
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        pa.terminate()


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw 16-bit mono PCM as an in-memory WAV blob."""
    import io
    import wave
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return bio.getvalue()


class DirectionWorker:
    """
    Runs one direction end to end: capture audio -> Live API -> emit transcripts.
    Reconnects on failure with backoff. Isolated from other directions.
    `emit(kind, direction_key, field, text, finished)` is the only output hook.
        kind  : "update" | "status" | "error"
        field : "orig" | "trans" (for "update")
    """

    def __init__(self, client, direction: Direction, emit, stop_event: threading.Event,
                 pause_event: threading.Event | None = None, live_model: str = MODEL):
        self.client = client
        self.d = direction
        self.emit = emit
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.live_model = live_model
        self._audio_q: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._audio_q = asyncio.Queue(maxsize=50)
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self.emit("status", self.d.key, None, "connecting…", False)
                cfg = build_config(self.d.target_language_code, self.live_model)
                async with self.client.aio.live.connect(model=self.live_model, config=cfg) as session:
                    self.emit("status", self.d.key, None, "● live", False)
                    backoff = 1.0
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._produce_audio())
                        tg.create_task(self._send_audio(session))
                        tg.create_task(self._receive(session))
                        tg.create_task(self._stopper())
            except Exception as eg:
                if self.stop_event.is_set():
                    break
                excs = eg.exceptions if isinstance(eg, BaseExceptionGroup) else (eg,)
                msg = "; ".join(sorted({repr(e) for e in excs}))
                self.emit("error", self.d.key, None, f"reconnecting ({msg})", False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
        self.emit("status", self.d.key, None, "stopped", False)

    async def _stopper(self):
        # Breaks the TaskGroup promptly when stop_event is set: raising cancels
        # the produce/send/receive tasks (which otherwise block on
        # session.receive()/_audio_q.get() forever), closes the Live session and
        # releases the mic stream. The outer loop then sees stop_event and exits.
        while not self.stop_event.is_set():
            await asyncio.sleep(0.1)
        raise _StopWorker

    # -- audio producers -------------------------------------------------------
    async def _produce_audio(self):
        if self.d.source == "wav":
            await self._produce_wav()
        else:
            await self._produce_mic()

    async def _produce_mic(self):
        async for frame in _mic_frames(self.stop_event, self.pause_event, self.d.device_index):
            await self._audio_q.put(frame)

    async def _produce_wav(self):
        wf = wave.open(self.d.wav_path, "rb")
        assert wf.getframerate() == SEND_SAMPLE_RATE and wf.getnchannels() == CHANNELS, (
            f"WAV must be {SEND_SAMPLE_RATE} Hz mono 16-bit; "
            f"got {wf.getframerate()} Hz / {wf.getnchannels()} ch"
        )
        while not self.stop_event.is_set():
            data = wf.readframes(CHUNK)
            if not data:
                break
            await self._audio_q.put(data)
            await asyncio.sleep(CHUNK / SEND_SAMPLE_RATE)  # pace ~ realtime
        await self._audio_q.put(None)  # sentinel: end of stream

    # -- send / receive --------------------------------------------------------
    async def _send_audio(self, session):
        from google.genai import types
        while not self.stop_event.is_set():
            data = await self._audio_q.get()
            if data is None:                       # wav finished
                await session.send_realtime_input(audio_stream_end=True)
                continue
            await session.send_realtime_input(
                audio=types.Blob(data=data, mime_type=AUDIO_MIME)
            )

    async def _receive(self, session):
        in_buf, out_buf = "", ""
        async for msg in session.receive():
            sc = getattr(msg, "server_content", None)
            if not sc:
                continue
            it = getattr(sc, "input_transcription", None)
            if it and it.text:
                in_buf += it.text
                self.emit("update", self.d.key, "orig", in_buf, bool(it.finished))
                if it.finished:
                    in_buf = ""
            ot = getattr(sc, "output_transcription", None)
            if ot and ot.text:
                out_buf += ot.text
                self.emit("update", self.d.key, "trans", out_buf, bool(ot.finished))
                if ot.finished:
                    out_buf = ""


class ChunkedWorker:
    """Cheap text-only engine. Segments the mic into utterances with an energy
    VAD, sends each as one audio chunk to a generate_content model, and streams
    back the translated text. No audio is generated and you pay per utterance
    (not per second of silence). Same emit/stop/pause contract as DirectionWorker
    so the relay/UI are unchanged.

    Long-speech handling: flush on a silence gap OR at MAX_UTTER_MS, and on a
    forced flush carry a short audio overlap into the next chunk so a mid-
    sentence cut keeps context.
    """
    SR = SEND_SAMPLE_RATE          # 16 kHz mono (from _mic_frames)
    RMS_THRESH = 500               # 16-bit energy threshold for "speech"
    SILENCE_HANG_MS = 700          # trailing silence that ends an utterance
    MAX_UTTER_MS = 7000            # force-flush long speech
    CARRY_MS = 400                 # overlap kept after a forced flush
    PREROLL_MS = 300               # audio kept before onset (avoid clipped starts)
    MIN_UTTER_MS = 250             # ignore blips

    def __init__(self, client, direction, emit, stop_event, pause_event, model):
        self.client = client
        self.d = direction
        self.emit = emit
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.model = model
        self._chunk_q: asyncio.Queue | None = None

    async def run(self):
        self._chunk_q = asyncio.Queue(maxsize=20)
        self.emit("status", self.d.key, None, "● live", False)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._capture_vad())
                tg.create_task(self._translate_loop())
                tg.create_task(self._stopper())
        except Exception as eg:
            if not self.stop_event.is_set():
                excs = eg.exceptions if isinstance(eg, BaseExceptionGroup) else (eg,)
                msg = "; ".join(sorted({repr(e) for e in excs}))
                self.emit("error", self.d.key, None, f"engine error ({msg})", False)
        self.emit("status", self.d.key, None, "stopped", False)

    async def _stopper(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(0.1)
        raise _StopWorker

    def _bpms(self) -> float:
        return self.SR * 2 / 1000.0    # bytes per ms, 16-bit mono

    async def _capture_vad(self):
        import audioop
        bpms = self._bpms()
        buf = bytearray()
        preroll = bytearray()
        speaking = False
        accum_ms = 0.0
        silence_ms = 0.0
        async for frame in _mic_frames(self.stop_event, self.pause_event, self.d.device_index):
            ms = len(frame) / bpms
            loud = audioop.rms(frame, 2) >= self.RMS_THRESH
            if loud:
                if not speaking:
                    speaking = True
                    buf.extend(preroll)             # include the onset we buffered
                buf.extend(frame)
                accum_ms += ms
                silence_ms = 0.0
                if accum_ms >= self.MAX_UTTER_MS:   # long run: flush, carry overlap
                    await self._queue_chunk(buf)
                    carry = bytes(buf[-int(self.CARRY_MS * bpms):])
                    buf = bytearray(carry)
                    accum_ms = self.CARRY_MS
                    silence_ms = 0.0
            else:
                preroll.extend(frame)               # rolling pre-roll of recent quiet
                cap = int(self.PREROLL_MS * bpms)
                if len(preroll) > cap:
                    del preroll[:-cap]
                if speaking:
                    buf.extend(frame)
                    accum_ms += ms
                    silence_ms += ms
                    if silence_ms >= self.SILENCE_HANG_MS:
                        await self._queue_chunk(buf)
                        buf = bytearray()
                        accum_ms = 0.0
                        silence_ms = 0.0
                        speaking = False
        if speaking:                                # stop: flush the tail
            await self._queue_chunk(buf)

    async def _queue_chunk(self, pcm):
        if len(pcm) / self._bpms() < self.MIN_UTTER_MS:
            return
        try:
            self._chunk_q.put_nowait(bytes(pcm))
        except asyncio.QueueFull:
            pass   # translator behind: skip to stay live

    async def _translate_loop(self):
        from google.genai import types
        tgt = self.d.target_language_code
        instr = (
            f"You are a real-time interpreter. Translate any human speech in the "
            f"audio into {tgt}. Output ONLY the translated text — no quotes, "
            f"labels, or notes. If the speech is already in {tgt}, return it "
            f"verbatim. If there is no clear human speech (silence, noise, music, "
            f"a tone, breathing), output nothing at all — an empty response.")
        cfg = types.GenerateContentConfig(
            system_instruction=instr, temperature=0.0,
            thinking_config=types.ThinkingConfig(thinking_budget=0))
        while not self.stop_event.is_set():
            pcm = await self._chunk_q.get()
            await self._translate_one(pcm, cfg, types)

    async def _translate_one(self, pcm, cfg, types):
        audio = types.Part.from_bytes(data=_pcm_to_wav(pcm, self.SR), mime_type="audio/wav")
        out = ""
        try:
            stream = await self.client.aio.models.generate_content_stream(
                model=self.model, contents=[audio], config=cfg)
            async for ch in stream:
                t = getattr(ch, "text", None)
                if t:
                    out += t
                    self.emit("update", self.d.key, "trans", out, False)
            if out.strip():
                self.emit("update", self.d.key, "trans", out.strip(), True)
        except Exception as e:
            self.emit("error", self.d.key, None, f"translate failed ({e!r})", False)


def _oai_lang(code: str) -> str:
    """OpenAI realtime-translate output language code (zh, en, nl, …)."""
    return (code or "en").split("-")[0].lower()


class OpenAIRealtimeWorker:
    """Streaming translation via OpenAI gpt-realtime-translate (Realtime API).
    Mic -> 24 kHz PCM16 base64 -> session.input_audio_buffer.append; reads
    session.output_transcript.delta (incremental fragments) -> emits 'trans'.
    There is no per-segment 'done' event, so a segment is finalized after
    FINALIZE_S of no new delta. Same emit/stop/pause/teardown contract as the
    other engines (TaskGroup + _stopper releases the mic on stop)."""
    SR = OPENAI_SR
    FINALIZE_S = 0.8               # seconds of no delta -> finalize the segment

    def __init__(self, direction: Direction, emit, stop_event: threading.Event,
                 pause_event: threading.Event | None = None,
                 model: str = OPENAI_TRANSLATE_MODEL):
        self.d = direction
        self.emit = emit
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.model = model
        self._seg = ""          # output (translated) transcript, current segment
        self._seg_in = ""       # input (source) transcript, current segment
        self._last_delta = 0.0

    async def run(self):
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            self.emit("error", self.d.key, None, "no OPENAI_API_KEY (add it to .env)", False)
            self.emit("status", self.d.key, None, "stopped", False)
            return
        url = f"wss://api.openai.com/v1/realtime/translations?model={self.model}"
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                import websockets
                self.emit("status", self.d.key, None, "connecting…", False)
                async with websockets.connect(
                        url, additional_headers={"Authorization": f"Bearer {key}"},
                        max_size=None) as ws:
                    # No input transcription: it spins up a 2nd model (whisper) that
                    # bills the FULL audio duration again -> doubles the cost. The
                    # source-text check stays free on Gemini (same session); on OpenAI
                    # we skip it to keep cost single-metered.
                    await ws.send(json.dumps({"type": "session.update", "session": {"audio": {
                        "output": {"language": _oai_lang(self.d.target_language_code)}}}}))
                    self.emit("status", self.d.key, None, "● live", False)
                    backoff = 1.0
                    self._seg = self._seg_in = ""
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._send_audio(ws))
                        tg.create_task(self._receive(ws))
                        tg.create_task(self._finalizer())
                        tg.create_task(self._stopper())
            except Exception as eg:
                if self.stop_event.is_set():
                    break
                excs = eg.exceptions if isinstance(eg, BaseExceptionGroup) else (eg,)
                msg = "; ".join(sorted({repr(e) for e in excs}))
                self.emit("error", self.d.key, None, f"reconnecting ({msg})", False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
        self.emit("status", self.d.key, None, "stopped", False)

    async def _stopper(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(0.1)
        raise _StopWorker

    async def _send_audio(self, ws):
        import base64
        async for frame in _mic_frames(self.stop_event, self.pause_event,
                                       self.d.device_index, out_rate=self.SR):
            await ws.send(json.dumps({"type": "session.input_audio_buffer.append",
                                      "audio": base64.b64encode(frame).decode()}))

    async def _receive(self, ws):
        loop = asyncio.get_running_loop()
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            t = m.get("type")
            if t == "session.output_transcript.delta":
                self._seg += m.get("delta", "")
                self._last_delta = loop.time()
                self.emit("update", self.d.key, "trans", self._seg, False)
            elif t == "session.input_transcript.delta":
                self._seg_in += m.get("delta", "")
                self._last_delta = loop.time()
                self.emit("update", self.d.key, "orig", self._seg_in, False)  # -> small grey "mine"
            elif t == "error":
                self.emit("error", self.d.key, None, str(m.get("error"))[:120], False)
            # session.output_audio.delta is ignored (we want text only)

    async def _finalizer(self):
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            await asyncio.sleep(0.2)
            if self._seg and (loop.time() - self._last_delta) >= self.FINALIZE_S:
                self.emit("update", self.d.key, "trans", self._seg.strip(), True)
                if self._seg_in:
                    self.emit("update", self.d.key, "orig", self._seg_in.strip(), True)
                self._seg = self._seg_in = ""


def make_client():
    from google import genai
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("ERROR: set GEMINI_API_KEY in your environment.")
    return genai.Client(http_options={"api_version": "v1beta"}, api_key=key)


def run_engine(directions: list[Direction], emit, stop_event: threading.Event,
               pause_event: threading.Event | None = None,
               engine: str = "chunked", model: str | None = None):
    """Run all directions concurrently in one asyncio loop (own thread).
    engine="chunked" = cheap audio->text (generate_content); "live" = streaming
    Live API. `model` is the model for the ACTIVE engine (defaults per engine)."""
    async def _main():
        if engine == "openai":
            om = model or OPENAI_TRANSLATE_MODEL
            workers = [OpenAIRealtimeWorker(d, emit, stop_event, pause_event, om)
                       for d in directions]
        elif engine == "live":
            client = make_client()
            lm = model or MODEL
            workers = [DirectionWorker(client, d, emit, stop_event, pause_event, live_model=lm)
                       for d in directions]
        else:
            client = make_client()
            cm = model or DEFAULT_CHUNK_MODEL
            workers = [ChunkedWorker(client, d, emit, stop_event, pause_event, cm)
                       for d in directions]
        await asyncio.gather(*(w.run() for w in workers))
    asyncio.run(_main())


# ---- GUI: the caption bar ----------------------------------------------------
def run_gui(directions: list[Direction]):
    import tkinter as tk

    ui_q: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    pause_event = threading.Event()

    def emit(kind, dkey, field_, text, finished):
        ui_q.put((kind, dkey, field_, text, finished))

    # palette
    BG = "#0d1117"          # text area
    BG_CTRL = "#161b22"     # control block
    BTN_BG = "#21262d"
    SEP = "#30363a"
    DIM = "#6e7681"
    HEAD = "#8b949e"
    CJK = "PingFang SC"     # renders both Latin and Chinese on macOS

    root = tk.Tk()
    root.title("TranslateBar")
    root.configure(bg=BG)
    try:
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.95)
    except tk.TclError:
        pass
    root.overrideredirect(True)  # borderless bar; still captured in full-screen share

    import tkinter.font as tkfont
    head_font = tkfont.Font(family=CJK, size=11, weight="bold")
    orig_font = tkfont.Font(family=CJK, size=12)
    trans_font = tkfont.Font(family=CJK, size=18, weight="bold")

    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    CTRL_W = 136
    bar_w = int(sw * 0.96)
    text_w = bar_w - CTRL_W - 44   # wraplength budget for the text labels

    # Height = header + LINES orig rows + LINES trans rows + breathing room.
    LINES = 2          # visible text lines per field (older lines scroll off top)
    lh = head_font.metrics("linespace")
    lo = orig_font.metrics("linespace")
    lt = trans_font.metrics("linespace")
    row_h = lh + LINES * lo + LINES * lt + 30
    bar_h = row_h * len(directions) + 8
    x = (sw - bar_w) // 2
    y = sh - bar_h - 48
    root.geometry(f"{bar_w}x{bar_h}+{x}+{y}")

    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", expand=True)

    # ---- right-hand control block (full height) ------------------------------
    ctrl = tk.Frame(body, bg=BG_CTRL, width=CTRL_W)
    ctrl.pack(side="right", fill="y")
    ctrl.pack_propagate(False)
    tk.Frame(body, bg=SEP, width=1).pack(side="right", fill="y")

    status_lbl = tk.Label(ctrl, text="…", fg=DIM, bg=BG_CTRL, font=(CJK, 13, "bold"))
    status_lbl.pack(pady=(16, 0))

    btns = tk.Frame(ctrl, bg=BG_CTRL)
    btns.pack(side="bottom", fill="x", pady=(0, 14))

    def mk_btn(text, cmd, fg="#c9d1d9"):
        b = tk.Label(btns, text=text, fg=fg, bg=BTN_BG, font=(CJK, 13, "bold"),
                     pady=7, cursor="pointinghand")
        b.pack(fill="x", padx=14, pady=3)
        b.bind("<Button-1>", lambda e: (cmd(), "break")[1])  # "break" => no drag
        return b

    paused = {"on": False}

    def toggle_pause():
        paused["on"] = not paused["on"]
        if paused["on"]:
            pause_event.set()
            pause_btn.config(text="▶ Hervat", fg="#3fb950")
        else:
            pause_event.clear()
            pause_btn.config(text="⏸ Pauze", fg="#c9d1d9")
        refresh_status()

    pause_btn = mk_btn("⏸ Pauze", toggle_pause)
    mk_btn("✕ Sluiten", lambda: shutdown(), fg="#f85149")

    # ---- left text area: one block per direction, using the full height ------
    left = tk.Frame(body, bg=BG)
    left.pack(side="left", fill="both", expand=True)

    rows: dict[str, dict] = {}
    dir_status: dict[str, str] = {}
    for i, d in enumerate(directions):
        frame = tk.Frame(left, bg=BG)
        frame.pack(side="top", fill="both", expand=True,
                   padx=16, pady=(10 if i == 0 else 4, 4))
        tk.Label(frame, text=d.title, fg=HEAD, bg=BG, font=head_font,
                 anchor="w").pack(fill="x")
        orig = tk.Label(frame, text="", fg=DIM, bg=BG, font=orig_font,
                        anchor="nw", justify="left", wraplength=text_w)
        orig.pack(fill="x")
        trans = tk.Label(frame, text="", fg=d.color, bg=BG, font=trans_font,
                         anchor="nw", justify="left", wraplength=text_w)
        trans.pack(fill="x", pady=(3, 0))
        rows[d.key] = {"orig": orig, "trans": trans,
                       "orig_final": "", "orig_partial": "",
                       "trans_final": "", "trans_partial": ""}
        dir_status[d.key] = "…"

    # ---- drag-to-move (whole window; buttons opt out via "break") ------------
    drag = {"x": 0, "y": 0}
    def start_drag(e): drag["x"], drag["y"] = e.x_root, e.y_root
    def do_drag(e):
        root.geometry(f"+{root.winfo_x()+e.x_root-drag['x']}"
                      f"+{root.winfo_y()+e.y_root-drag['y']}")
        drag["x"], drag["y"] = e.x_root, e.y_root
    root.bind("<Button-1>", start_drag)
    root.bind("<B1-Motion>", do_drag)
    root.bind("<Escape>", lambda e: shutdown())

    def shutdown():
        stop_event.set()
        root.after(150, root.destroy)

    # ---- typewriter scroll: break into whole lines, show the newest LINES;
    #      older lines scroll off the top, a finished line never reflows --------
    MAXBUF = 600  # chars kept per field (bounds re-wrap cost over a long meeting)

    def wrap_lines(text: str, font) -> list[str]:
        """Greedy wrap to the text column: words for Latin, per-char for CJK."""
        lines, cur = [], ""
        for word in text.split(" "):
            if not word:
                continue
            trial = f"{cur} {word}".strip() if cur else word
            if font.measure(trial) <= text_w:
                cur = trial
                continue
            if cur:
                lines.append(cur)
            if font.measure(word) <= text_w:
                cur = word
            else:                                  # long CJK run (no spaces)
                cur = ""
                for ch in word:
                    if font.measure(cur + ch) <= text_w:
                        cur += ch
                    else:
                        lines.append(cur)
                        cur = ch
        if cur:
            lines.append(cur)
        return lines

    def render(r, field_):
        font = trans_font if field_ == "trans" else orig_font
        buf = (r[f"{field_}_final"] + " " + r[f"{field_}_partial"]).strip()
        r[field_].config(text="\n".join(wrap_lines(buf, font)[-LINES:]))

    def refresh_status():
        if paused["on"]:
            status_lbl.config(text="❚❚ pauze", fg="#d29922")
            return
        vals = list(dir_status.values())
        if any(("reconnect" in v or "error" in v) for v in vals):
            txt = next(v for v in vals if "reconnect" in v or "error" in v)
            status_lbl.config(text="● probleem", fg="#f85149")
        elif any("connect" in v for v in vals):
            status_lbl.config(text="connecting…", fg="#d29922")
        elif any("live" in v for v in vals):
            status_lbl.config(text="● live", fg="#3fb950")
        elif any("stopped" in v for v in vals):
            status_lbl.config(text="stopped", fg=DIM)
        else:
            status_lbl.config(text="…", fg=DIM)

    def poll():
        try:
            while True:
                kind, dkey, field_, text, finished = ui_q.get_nowait()
                r = rows.get(dkey)
                if kind in ("status", "error"):
                    dir_status[dkey] = text
                    refresh_status()
                elif kind == "update" and r is not None:
                    if finished:
                        merged = (r[f"{field_}_final"] + " " + text).strip()
                        r[f"{field_}_final"] = merged[-MAXBUF:]
                        r[f"{field_}_partial"] = ""
                    else:
                        r[f"{field_}_partial"] = text[-MAXBUF:]
                    render(r, field_)
        except queue.Empty:
            pass
        root.after(40, poll)

    worker = threading.Thread(
        target=run_engine, args=(directions, emit, stop_event, pause_event),
        daemon=True,
    )
    worker.start()
    poll()
    root.protocol("WM_DELETE_WINDOW", shutdown)
    root.mainloop()
    stop_event.set()


# ---- CLI helpers -------------------------------------------------------------
def list_devices():
    import pyaudio
    pa = pyaudio.PyAudio()
    print("Audio input devices (use the index or a name substring):\n")
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0:
            print(f"  [{i:2}] {info['name']}  "
                  f"(in={info['maxInputChannels']}, {int(info['defaultSampleRate'])} Hz)")
    pa.terminate()


def list_input_devices() -> list[dict]:
    """Structured input-device list for the settings UI: [{index, name}]."""
    import pyaudio
    pa = pyaudio.PyAudio()
    out = []
    try:
        try:
            default_idx = pa.get_default_input_device_info()["index"]
        except Exception:
            default_idx = None
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                out.append({"index": i, "name": info["name"],
                            "default": i == default_idx})
    finally:
        pa.terminate()
    return out


def resolve_device(spec: str | None) -> int | None:
    if spec is None:
        return None
    if spec.isdigit():
        return int(spec)
    import pyaudio
    pa = pyaudio.PyAudio()
    try:
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0 and spec.lower() in info["name"].lower():
                return i
    finally:
        pa.terminate()
    raise SystemExit(f"No input device matching {spec!r}. Try --list-devices.")


def find_device(substr: str) -> int | None:
    """Return the index of the first input device whose name contains substr."""
    import pyaudio
    pa = pyaudio.PyAudio()
    try:
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0 and substr.lower() in info["name"].lower():
                return i
    finally:
        pa.terminate()
    return None


def check():
    """No-network structural self-test."""
    ok = True
    try:
        import google.genai  # noqa
        from google.genai import types
        for code in (DEFAULT_TARGET_THEM, DEFAULT_TARGET_YOU):
            cfg = build_config(code)
            assert cfg.translation_config.target_language_code == code
        print("✓ google-genai import + LiveConnectConfig build OK")
    except Exception as e:
        ok = False
        print(f"✗ google-genai / config: {e!r}")
    try:
        import pyaudio  # noqa
        print("✓ pyaudio import OK")
    except Exception as e:
        ok = False
        print(f"✗ pyaudio import failed: {e!r}  (pip install pyaudio; brew install portaudio)")
    try:
        import tkinter  # noqa
        print("✓ tkinter import OK")
    except Exception as e:
        ok = False
        print(f"✗ tkinter import failed: {e!r}")
    print("KEY present:", bool(os.environ.get("GEMINI_API_KEY")))
    print("RESULT:", "READY" if ok else "MISSING DEPENDENCIES")
    return ok


def selftest(wav_path: str, direction_key: str, target: str):
    """End-to-end, no GUI: stream a WAV and print transcripts."""
    d = Direction(
        key=direction_key.upper(),
        title="selftest",
        target_language_code=target,
        color="",
        source="wav",
        wav_path=wav_path,
    )
    stop_event = threading.Event()

    def emit(kind, dkey, field_, text, finished):
        if kind in ("status", "error"):
            print(f"[{dkey}] {text}")
        elif kind == "update" and finished:
            tag = "orig " if field_ == "orig" else "TRANS"
            print(f"[{dkey}] {tag}: {text}")

    # stop shortly after the wav drains
    def watchdog():
        import time
        time.sleep(float(os.environ.get("SELFTEST_SECONDS", "30")))
        stop_event.set()
    threading.Thread(target=watchdog, daemon=True).start()
    run_engine([d], emit, stop_event)


# ---- main --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Live EN<->ZH caption bar (Gemini 3.5 Live Translate)")
    ap.add_argument("--list-devices", action="store_true", help="list audio input devices and exit")
    ap.add_argument("--check", action="store_true", help="no-network dependency/config self-test")
    ap.add_argument("--selftest", metavar="WAV", help="stream a 16kHz mono WAV and print transcripts")
    ap.add_argument("--direction", choices=["in", "out"], default="out",
                    help="selftest direction (default: out)")
    ap.add_argument("--mic-device", metavar="N|NAME", default=None,
                    help="input device for your voice (default: system default mic)")
    ap.add_argument("--in-device", metavar="N|NAME", default=None,
                    help="input device carrying the MEET audio (e.g. 'BlackHole'); enables IN")
    ap.add_argument("--no-out", action="store_true", help="disable the mic->Chinese (OUT) lane")
    ap.add_argument("--target-them", default=DEFAULT_TARGET_THEM, help="OUT target lang (default zh-Hans)")
    ap.add_argument("--target-you", default=DEFAULT_TARGET_YOU, help="IN target lang (default en)")
    args = ap.parse_args()

    if args.list_devices:
        return list_devices()
    if args.check:
        sys.exit(0 if check() else 1)
    if args.selftest:
        target = args.target_you if args.direction == "in" else args.target_them
        return selftest(args.selftest, args.direction, target)

    directions: list[Direction] = []
    if not args.no_out:
        directions.append(Direction(
            key="OUT", title="🎤 YOU → 中文", target_language_code=args.target_them,
            color="#79c0ff", source="mic", device_index=resolve_device(args.mic_device),
        ))
    # IN lane: use --in-device if given, else auto-enable when BlackHole exists.
    in_idx = resolve_device(args.in_device) if args.in_device is not None \
        else find_device("BlackHole")
    if in_idx is not None:
        directions.append(Direction(
            key="IN", title="🎧 THEM → EN", target_language_code=args.target_you,
            color="#7ee787", source="device", device_index=in_idx,
        ))
    if not directions:
        raise SystemExit("Nothing to do: enable OUT (default) and/or pass --in-device.")
    run_gui(directions)


if __name__ == "__main__":
    main()
