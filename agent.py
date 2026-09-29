"""
SpeakEasy - real-time voice agent (AssemblyAI Voice Agent API)

Same pipeline as before (mic -> AssemblyAI -> speaker + tool calls), but:
  * speaker playback runs in its own thread (no more blocking the event loop)
  * tool errors can never leave the agent hanging
  * tool arguments / results are parsed and serialised safely
  * clean shutdown when the connection drops or you press Ctrl+C
  * multilingual (English / Hindi / Marathi / Hinglish) + clarification prompt
  * works with your current tools.py (CONTROL_FAN_TOOL) or a future TOOLS list
"""

import asyncio
import base64
import http.server
import json
import os
import queue
import re
import sys
import threading
import webbrowser
from datetime import datetime

import numpy as np
import pyaudio
import sounddevice as sd
import websockets
from dotenv import load_dotenv

# Optional: Hindi/Marathi speech (AssemblyAI cannot speak these itself).
#   pip install edge-tts miniaudio
try:
    import edge_tts
    import miniaudio

    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False

# Works with your current tools.py, and with a future one that exposes TOOLS.
try:
    from tools import TOOLS, dispatch_tool
except ImportError:
    from tools import CONTROL_FAN_TOOL, dispatch_tool

    TOOLS = [CONTROL_FAN_TOOL]


# Print Hindi/Marathi (Devanagari) as proper UTF-8 on Windows
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# =========================================================
# SETTINGS
# =========================================================

load_dotenv()

API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
if not API_KEY:
    raise ValueError("ASSEMBLYAI_API_KEY not found in .env")

WS_URL = "wss://agents.assemblyai.com/v1/ws"

SAMPLE_RATE = 24000
CHANNELS = 1
CHUNK = 1200  # 50 ms of audio at 24 kHz
# anna/charles/paul/vera are BRITISH voices. For clear neutral English try:
# jane, alba, eve, george, jean, mary, michael  (set VOICE=... in .env)
VOICE = os.getenv("VOICE", "jane")

# AssemblyAI's own voice speaks English well but NOT Hindi/Marathi.
# So for Hindi/Marathi we take the agent's TEXT reply and speak it with
# Microsoft neural voices (edge-tts). English keeps using AssemblyAI audio.
LANG_VOICES = {
    "en": "en-IN-NeerjaNeural",  # only used if an English reply must be spoken locally
    "hi": "hi-IN-SwaraNeural",
    "mr": "mr-IN-AarohiNeural",
}
TTS_RATE = "-8%"      # slightly slower = clearer Hindi/Marathi (try "-15%" for slower)
UI_PORT = 8765        # transcript page: http://127.0.0.1:8765
LANG_NAMES = {"en": "English", "hi": "Hindi", "mr": "Marathi"}
CURRENT_LANG = "en"  # updated automatically from what the user speaks

# Words that tell Marathi and Hindi apart (both are written in Devanagari).
# Marathi speech is sometimes transcribed with Hindi-looking words, so the
# language "sticks": on a tie the previous Hindi/Marathi choice is kept.
MARATHI_WORDS = {
    "मला", "तुला", "आहे", "आहेत", "आहात", "नाही", "नको", "काय", "करा", "करू",
    "करतो", "करते", "मराठी", "मराठीत", "आणि", "तुम्ही", "आम्ही", "पाहिजे",
    "हवे", "हवा", "हवी", "सांग", "सांगा", "बोला", "कसा", "कसे", "कशी",
    "माझा", "माझी", "माझे", "तुमचा", "तुमची", "तुझा", "तुझी", "मी", "होते",
    "झाले", "झाला", "केला", "केले", "इथे", "येथे", "म्हणून", "पण", "का",
}
HINDI_WORDS = {
    "है", "हैं", "हूँ", "हूं", "मैं", "में", "क्या", "नहीं", "करो", "करें",
    "आप", "और", "को", "की", "के", "हिंदी", "हिन्दी", "कृपया", "सकता",
    "सकते", "सकती", "रहा", "रही", "चाहिए", "बताओ", "बोलो", "तुम", "मुझे",
    "आपको", "यह", "वह", "था", "थी", "मेरा", "मेरी", "आपका", "आपकी", "बात",
}


def detect_language(text: str, current: str, explicit: bool = True) -> str:
    """Pick the reply language (en / hi / mr) from what the user said."""
    low = text.lower()

    # Explicit requests win (only checked on what the USER said)
    if explicit:
        if "मराठी" in text or "marathi" in low:
            return "mr"
        if "हिंदी" in text or "हिन्दी" in text or "hindi" in low:
            return "hi"
        if "english" in low or "इंग्लिश" in text or "अंग्रेजी" in text or "अंग्रेज़ी" in text:
            return "en"

    letters = [c for c in text if c.isalpha()]
    if not letters:
        return current
    dev = sum(1 for c in letters if "\u0900" <= c <= "\u097f")
    if dev / len(letters) < 0.3:
        return "en"  # Latin script (English or romanised Hinglish)

    tokens = re.findall(r"[\u0900-\u097f]+", text)
    mr = sum(t in MARATHI_WORDS for t in tokens)
    hi = sum(t in HINDI_WORDS for t in tokens)
    if mr > hi:
        return "mr"
    if hi > mr:
        return "hi"
    return current if current in ("hi", "mr") else "hi"

TRANSCRIPT_FILE = "transcript.txt"  # UTF-8 log, readable even if the console isn't
# Words the speech recogniser should listen for (device names, Hindi commands)
KEYTERMS = [
    "SpeakEasy", "fan", "light", "on", "off", "turn on", "turn off",
    "pankha", "chalu", "band", "बंद", "चालू", "पंखा", "लाइट", "बत्ती",
]
GREETING = "Hello! I am SpeakEasy. How can I help you?"


# =========================================================
# SYSTEM PROMPT
# =========================================================

SYSTEM_PROMPT = """
You are SpeakEasy, an intelligent, friendly, accessibility-aware real-time
voice assistant.

VOICE BEHAVIOR:
- Keep every reply to one or two short sentences: it is spoken aloud.
- Sound natural and conversational. No lists, no markdown, no emojis.
- Focus on the user's intended meaning, not their exact wording.

LANGUAGE:
- Always reply in the SAME language the user used in their latest message:
  English -> English; Hindi -> Hindi; Marathi -> Marathi.
- Write Hindi and Marathi in Devanagari script only. Use simple everyday words,
  short sentences, correct punctuation, and NO English words, digits or symbols
  (write numbers as words) so the text-to-speech voice pronounces it clearly.
- If the user writes romanised Hinglish (e.g. "fan on kar na"), reply in English.
- If the user explicitly asks for a language ("हिंदी में बोल", "मराठीत बोला",
  "speak English"), use it and keep using it until they ask for another.
- Marathi speech is sometimes transcribed with Hindi-looking words. If the
  conversation so far has been in Marathi, keep replying in Marathi.
- If a transcript looks like nonsense, do not guess: ask the user to repeat.
- Tool arguments must ALWAYS use the English values from the tool schema,
  whatever language the user spoke.

ACCESSIBILITY:
- Be tolerant of accents, dialects, stammering, filler words and
  incomplete sentences.
- Never require special phrasing. "fan on", "start the fan", "fan chalu kar",
  "पंखा चालू करो" and "can you switch the fan on please" all mean the same thing.
- If the intent is clear, act immediately even when the wording is unusual.

CLARIFICATION:
- If the request is ambiguous ("turn it on", "start it") or a required
  parameter is missing, ask ONE short clarifying question.
- Never guess a device or a value.

TOOL USAGE:
- Use the matching tool whenever the user asks for an action.
- Provide valid arguments that follow the tool schema exactly.
- Never invent required parameters.
- Never claim an action succeeded unless the tool result confirms it.
- If a tool reports an error, tell the user briefly and offer to retry.
""".strip()


# =========================================================
# AUDIO OUTPUT (own thread, so playback never blocks asyncio)
# =========================================================

class Speaker:
    def __init__(self):
        self._queue = queue.Queue()
        self._stream = sd.OutputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", latency="high"
        )
        self._stream.start()
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self):
        while self._running:
            chunk = self._queue.get()
            if chunk is None:
                break
            try:
                self._stream.write(chunk)
            except Exception:
                # Happens when flush() aborts the stream mid-write.
                pass

    def play(self, pcm_bytes: bytes):
        self._queue.put(np.frombuffer(pcm_bytes, dtype=np.int16))

    def flush(self):
        """Drop everything not yet played (used when the user interrupts)."""
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._stream.abort()
            self._stream.start()
        except Exception:
            pass

    def close(self):
        self._running = False
        self._queue.put(None)
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass


# =========================================================
# TOOL EXECUTION
# =========================================================

def run_tool(name, arguments):
    """Run a tool; ALWAYS returns something serialisable, never raises.
    (If a tool.result is never sent, the agent would wait forever.)"""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return {"error": f"Invalid JSON arguments: {arguments}"}

    try:
        return dispatch_tool(name, arguments or {})
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def serialise(result) -> str:
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


TRANSCRIPT = []  # shown on the browser page


def log_line(speaker_label, text):
    """Save to a UTF-8 file and to the browser page (proper Devanagari fonts)."""
    TRANSCRIPT.append({"who": speaker_label, "text": text, "lang": CURRENT_LANG})
    try:
        with open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%H:%M:%S}] {speaker_label}: {text}\n")
    except Exception:
        pass


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>SpeakEasy</title>
<style>
body{margin:0;font-family:"Nirmala UI","Noto Sans Devanagari","Mangal","Segoe UI",system-ui,sans-serif;
background:#0f172a;color:#e2e8f0}
header{padding:16px 24px;background:#1e293b;display:flex;justify-content:space-between;align-items:center}
h1{margin:0;font-size:22px}#lang{background:#38bdf8;color:#0f172a;padding:4px 12px;border-radius:99px;font-weight:600}
#chat{max-width:820px;margin:0 auto;padding:24px;display:flex;flex-direction:column;gap:14px}
.m{padding:14px 18px;border-radius:16px;font-size:24px;line-height:1.6;max-width:85%}
.You{align-self:flex-end;background:#2563eb}.SpeakEasy{align-self:flex-start;background:#334155}
.who{font-size:13px;opacity:.7;margin-bottom:2px}
</style></head><body>
<header><h1>&#127908; SpeakEasy</h1><span id="lang">English</span></header>
<div id="chat"></div>
<script>
let n=-1;
async function tick(){try{
 const d=await (await fetch('/data')).json();
 document.getElementById('lang').textContent=d.lang_name;
 if(d.messages.length!==n){n=d.messages.length;
  const c=document.getElementById('chat');c.innerHTML='';
  d.messages.forEach(m=>{const e=document.createElement('div');e.className='m '+m.who;
   const w=document.createElement('div');w.className='who';w.textContent=m.who==='You'?'You':'SpeakEasy';
   const t=document.createElement('div');t.textContent=m.text;e.append(w,t);c.append(e)});
  window.scrollTo(0,document.body.scrollHeight);}
}catch(e){} setTimeout(tick,400);}
tick();
</script></body></html>"""


class UIHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/data"):
            body = json.dumps(
                {
                    "lang": CURRENT_LANG,
                    "lang_name": LANG_NAMES[CURRENT_LANG],
                    "messages": TRANSCRIPT,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        else:
            body, ctype = PAGE.encode("utf-8"), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_ui():
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", UI_PORT), UIHandler)
    except OSError:
        print(f"ℹ️  Transcript page not started (port {UI_PORT} busy)")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{UI_PORT}"
    print(f"🖥️  Transcript page: {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass


# =========================================================
# SEND MICROPHONE AUDIO
# =========================================================

async def send_audio(ws, microphone):
    print("🎤 Microphone started. Speak now! (headphones recommended)\n")
    loop = asyncio.get_running_loop()

    while True:
        data = await loop.run_in_executor(
            None, lambda: microphone.read(CHUNK, exception_on_overflow=False)
        )
        await ws.send(
            json.dumps({
                "type": "input.audio",
                "audio": base64.b64encode(data).decode("utf-8"),
            })
        )


# =========================================================
# RECEIVE ASSEMBLYAI EVENTS
# =========================================================

def clean_for_speech(text: str) -> str:
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"[\U00010000-\U0010ffff\u2600-\u27bf]", "", text)  # emojis
    return text.strip()


async def synthesize(text: str, lang: str) -> bytes:
    """Hindi/Marathi text -> 24 kHz mono int16 PCM."""
    communicate = edge_tts.Communicate(text, LANG_VOICES[lang], rate=TTS_RATE)
    mp3 = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3 += chunk["data"]
    decoded = await asyncio.to_thread(
        miniaudio.decode,
        mp3,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=SAMPLE_RATE,
    )
    return decoded.samples.tobytes()


async def tts_worker(tts_queue, speaker):
    """Speaks Hindi/Marathi replies in order."""
    while True:
        lang, text = await tts_queue.get()
        try:
            speaker.play(await synthesize(text, lang))
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  TTS failed ({type(exc).__name__}: {exc})")


def stop_local_speech(tts_queue, speaker):
    while not tts_queue.empty():
        tts_queue.get_nowait()
    speaker.flush()


async def receive_messages(ws, speaker, tts_queue):
    global CURRENT_LANG
    # Tool results are held here and sent only after reply.done.
    pending_tools = []

    async for raw_message in ws:
        event = json.loads(raw_message)
        event_type = event.get("type")

        if event_type == "transcript.user":
            text = event.get("text", "").strip()
            if text:
                print(f"\n🎤 You: {text}")
                log_line("You", text)
                new_lang = detect_language(text, CURRENT_LANG)
                if new_lang != CURRENT_LANG:
                    CURRENT_LANG = new_lang
                    print(f"🌐 Reply language: {LANG_NAMES[CURRENT_LANG]}")

        elif event_type == "transcript.agent":
            text = event.get("text", "").strip()
            if text:
                print(f"🤖 SpeakEasy: {text}")
                log_line("SpeakEasy", text)
                if TTS_AVAILABLE:
                    spoken = clean_for_speech(text)
                    reply_lang = detect_language(spoken, CURRENT_LANG, explicit=False)
                    if reply_lang != "en" and CURRENT_LANG == "en":
                        # Agent answered in Hindi/Marathi but English audio may
                        # already be playing: cut it and switch to the local voice.
                        speaker.flush()
                        CURRENT_LANG = reply_lang
                        print(f"🌐 Reply language: {LANG_NAMES[CURRENT_LANG]}")
                    if CURRENT_LANG != "en" and spoken:
                        await tts_queue.put((reply_lang, spoken))

        elif event_type == "reply.audio":
            # In Hindi/Marathi mode the English voice would mispronounce
            # Devanagari text, so its audio is dropped and edge-tts speaks.
            audio_data = event.get("data")
            if audio_data and (CURRENT_LANG == "en" or not TTS_AVAILABLE):
                speaker.play(base64.b64decode(audio_data))

        elif event_type == "tool.call":
            name = event.get("name")
            arguments = event.get("arguments", {})
            call_id = event.get("call_id")

            print("\n🔧 TOOL CALL")
            print("----------------------------------------")
            print(f"Tool: {name}")
            print(f"Arguments: {arguments}")

            # dispatch_tool is synchronous; keep it off the event loop
            result = await asyncio.to_thread(run_tool, name, arguments)
            print(f"Result: {result}")

            pending_tools.append({"call_id": call_id, "name": name, "result": result})

        elif event_type == "reply.done":
            if event.get("status") == "interrupted":
                pending_tools.clear()
                stop_local_speech(tts_queue, speaker)

            elif pending_tools:
                for tool in pending_tools:
                    result = tool["result"]
                    await ws.send(
                        json.dumps({
                            "type": "tool.result",
                            "call_id": tool["call_id"],
                            "result": serialise(result),
                        })
                    )
                    print("✅ Tool result sent")
                pending_tools.clear()

            print("----------------------------------------")

        elif event_type == "input.speech.started":
            # Barge-in for the local Hindi/Marathi voice
            if CURRENT_LANG != "en":
                stop_local_speech(tts_queue, speaker)

        elif event_type == "input.speech.stopped":
            pass

        elif event_type in ("error", "session.error"):
            print(f"\n❌ AssemblyAI Error: {event}")


# =========================================================
# MAIN
# =========================================================

async def main(microphone, speaker):
    print("\n========================================")
    print("       🎙️ SPEAKEASY VOICE AGENT")
    print("========================================\n")
    start_ui()

    headers = {"Authorization": f"Bearer {API_KEY}"}

    async with websockets.connect(
        WS_URL,
        additional_headers=headers,
        ping_interval=20,
        ping_timeout=60,
    ) as ws:

        await ws.send(
            json.dumps({
                "type": "session.update",
                "session": {
                    "system_prompt": SYSTEM_PROMPT,
                    "greeting": GREETING,
                    "tools": TOOLS,
                    "input": {
                        "format": {"encoding": "audio/pcm"},
                        "keyterms": KEYTERMS,
                        "turn_detection": {
                            "vad_threshold": 0.5,
                            "min_silence": 200,
                            "max_silence": 1000,
                            "interrupt_response": True,
                        },
                    },
                    "output": {
                        "voice": VOICE,
                        "format": {"encoding": "audio/pcm"},
                    },
                },
            })
        )

        print("Connecting to AssemblyAI...")

        # Wait for session.ready (fail loudly on errors / timeout)
        async def wait_ready():
            while True:
                event = json.loads(await ws.recv())
                etype = event.get("type")
                if etype == "session.ready":
                    return
                if etype in ("error", "session.error"):
                    raise RuntimeError(f"Session setup failed: {event}")

        await asyncio.wait_for(wait_ready(), timeout=20)

        print("🟢 Connected to AssemblyAI")
        print("🟢 Voice Agent is ready")
        print("🛑 Press Ctrl+C to stop")
        print("----------------------------------------")

        tts_queue = asyncio.Queue()
        if not TTS_AVAILABLE:
            print("ℹ️  Hindi/Marathi speech disabled: pip install edge-tts miniaudio")
        tasks = [
            asyncio.create_task(send_audio(ws, microphone)),
            asyncio.create_task(receive_messages(ws, speaker, tts_queue)),
            asyncio.create_task(tts_worker(tts_queue, speaker)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                t.result()  # re-raise the failure, if any
        finally:
            for t in tasks:
                t.cancel()


# =========================================================
# START PROGRAM
# =========================================================

if __name__ == "__main__":
    mic_audio = pyaudio.PyAudio()
    microphone = mic_audio.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )
    speaker = Speaker()

    try:
        asyncio.run(main(microphone, speaker))
    except KeyboardInterrupt:
        print("\n\n🛑 SpeakEasy stopped.")
    except websockets.ConnectionClosed as exc:
        print(f"\n🔌 Connection closed: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ Fatal error: {type(exc).__name__}: {exc}")
    finally:
        try:
            microphone.stop_stream()
            microphone.close()
            mic_audio.terminate()
        except Exception:
            pass
        speaker.close()