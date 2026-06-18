# TranslateBar

A small desktop caption bar for **live two-way translated meetings**. It runs
*next to* your video call (Google Meet, Zoom, anything). Each side translates
its **own microphone** with Google Gemini and sends only the **subtitle text**
to the other side over a tiny relay — so you read, big and green, what the other
person says, already in your language.

It does **not** plug into the meeting itself; it's an overlay you run alongside it.

---

## One-time setup (~10 minutes)

You need **Python 3.10–3.12** and your own **Google Gemini API key**
(free at <https://aistudio.google.com/apikey>).

```bash
# 1. get the code
git clone https://github.com/rheijmen/translatebar.git
cd translatebar

# 2. create an environment + install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Microphone dependency (PyAudio):**
- **macOS:** `brew install portaudio` *before* the pip install above.
- **Windows:** the pip wheel bundles it; if `pip install pyaudio` fails, run
  `pip install pipwin && pipwin install pyaudio`. The bar uses Microsoft Edge
  **WebView2** (preinstalled on Windows 11).
- **Python 3.13+:** also run `pip install audioop-lts`.

**To host a meeting** you also need Cloudflare's free tunnel tool (no account):
- **macOS:** `brew install cloudflared`
- **Windows:** download from
  <https://developer.microsoft.com/en-us/microsoft-edge> … see
  <https://github.com/cloudflare/cloudflared/releases>.

> Joining a meeting doesn't need `cloudflared` — only the person who hosts.

---

## Running it

- **macOS:** double-click **`TranslateBar.command`** (or `.venv/bin/python duo_web.py`).
- **Windows:** `.venv\Scripts\python duo_web.py`

Allow **microphone** access when your OS asks.

On launch a short **wizard** appears:

1. **First time only:** paste your Gemini API key (saved locally in `.env`).
2. **Connect** — *"Are you starting this meeting, or joining one?"*
   - **I'm starting it (host):** the app opens a public relay and shows an
     **invitation** — click **Copy invitation** and send it to the other side.
   - **I'm joining:** paste the invitation the host sent you (the whole message
     is fine — it finds the link).
3. **Almost there** — your name, the language to translate *your* speech into
   (what the other side reads), and your microphone. These are remembered.

Then click **Start**. Your name, language and mic are saved for next time, so
later meetings are just *Connect → Start*. Change anything later with the ⚙
button on the bar.

---

## In the bar

- **Big green text** — what the other person said, in your language.
- **Small dim text** — your own words (a check that you were heard right).
- **Right panel:** play/pause, a link indicator (green = connected, pulsing =
  pairing), settings (⚙), and power (click twice to close).

---

## How a meeting connects

```
   YOU                                            OTHER SIDE
 ┌──────────────┐                               ┌──────────────┐
 │ video call   │  ← normal Meet/Zoom audio →   │ video call   │
 ├──────────────┤                               ├──────────────┤
 │ TranslateBar │  your mic → Gemini → text     │ TranslateBar │
 │              │ ───┐                      ┌──  │              │
 │              │    │     ┌──────────┐     │    │              │
 │              │    └───→ │  relay   │ ←───┘    │              │
 └──────────────┘  text    │ (host's) │  text    └──────────────┘
                           └──────────┘
```

The host runs the relay (via **Start hosting**) and shares one link; that link
already contains the room, so the other side just pastes it. The link is a
`wss://…trycloudflare.com#room` address — it's a **connection link you paste
into the app**, not a website.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Link indicator keeps pulsing | Both sides must use the **same invitation**. The host's link changes each time they restart hosting — use a fresh one. |
| No captions when you speak | Microphone permission not granted, or no/!invalid Gemini key (open ⚙). |
| `ModuleNotFoundError: audioop` | Python 3.13+ → `pip install audioop-lts`. |
| PyAudio install error | macOS: `brew install portaudio` first. Windows: `pipwin install pyaudio`. |
| "Start hosting" fails | Install `cloudflared` (see setup) and check your network. |

---

*A lightweight bridge until video platforms ship native Chinese live translation.*
