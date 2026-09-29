import json
import os
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Import the real Python tools
from tools import TOOLS, dispatch_tool


# ============================================================
# LOAD ENVIRONMENT
# ============================================================

ROOT = Path(__file__).resolve().parent


def load_env():
    env_file = ROOT / ".env"

    if not env_file.exists():
        return

    for line in env_file.read_text(encoding="utf-8").splitlines():

        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)

        key = key.strip()
        value = value.strip().strip('"').strip("'")

        os.environ.setdefault(key, value)


load_env()


API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
PORT = int(os.getenv("PORT", "3000"))


if not API_KEY:
    raise RuntimeError(
        "ASSEMBLYAI_API_KEY is missing from .env"
    )


# ============================================================
# SPEAKEASY CONFIGURATION
# ============================================================

SYSTEM_PROMPT = """
You are SpeakEasy, an intelligent, friendly, accessibility-aware real-time voice assistant.

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
- If Marathi speech is sometimes transcribed with Hindi-looking words, keep the
  conversation language consistent.
- If a transcript looks like nonsense, do not guess: ask the user to repeat.
- Tool arguments must ALWAYS use the English values from the tool schema,
  whatever language the user spoke.

ACCESSIBILITY:
- Be tolerant of accents, dialects, stammering, filler words and incomplete sentences.
- Never require special phrasing. "fan on", "start the fan", "fan chalu kar",
  "पंखा चालू करो", "light on", "turn on the light", "लाइट चालू करो"
  and "बत्ती चालू करा" all mean the same thing.
- If the intent is clear, act immediately even when the wording is unusual.

CLARIFICATION:
- If the request is ambiguous ("turn it on", "start it") or a required
  parameter is missing, ask ONE short clarifying question.
- Never guess a device or a value.

TOOL USAGE:
- Use the matching tool whenever the user asks for an action.
- For fan requests, use control_fan.
- For light requests, use control_light.
- Provide valid arguments that follow the tool schema exactly.
- Never invent required parameters.
- Never claim an action succeeded unless the tool result confirms it.
- If a tool reports an error, tell the user briefly and offer to retry.
""".strip()


GREETING = "Hello! I am SpeakEasy. How can I help you?"

VOICE = os.getenv("VOICE", "jane")


# ============================================================
# KEYTERMS
# ============================================================

KEYTERMS = [
    "SpeakEasy",

    # Fan
    "fan",
    "pankha",
    "पंखा",

    # Light
    "light",
    "लाइट",
    "batti",
    "बत्ती",

    # Actions
    "on",
    "off",
    "turn on",
    "turn off",
    "switch on",
    "switch off",

    # Hindi / Marathi
    "chalu",
    "band",
    "चालू",
    "बंद",
]


# ============================================================
# JSON RESPONSE
# ============================================================

def json_response(handler, status, data):

    body = json.dumps(
        data,
        ensure_ascii=False
    ).encode("utf-8")

    handler.send_response(status)

    handler.send_header(
        "Content-Type",
        "application/json; charset=utf-8"
    )

    handler.send_header(
        "Content-Length",
        str(len(body))
    )

    handler.send_header(
        "Cache-Control",
        "no-store"
    )

    handler.end_headers()

    handler.wfile.write(body)


# ============================================================
# WEB SERVER
# ============================================================

class Handler(BaseHTTPRequestHandler):

    # --------------------------------------------------------
    # GET REQUESTS
    # --------------------------------------------------------

    def do_GET(self):

        path = urllib.parse.urlparse(
            self.path
        ).path


        # ----------------------------------------------------
        # CREATE TEMPORARY ASSEMBLYAI TOKEN
        # ----------------------------------------------------

        if path == "/api/voice-token":

            try:

                url = (
                    "https://agents.assemblyai.com/v1/token"
                    "?expires_in_seconds=300"
                )

                request = urllib.request.Request(
                    url,
                    headers={
                        "Authorization": f"Bearer {API_KEY}"
                    },
                    method="GET",
                )

                with urllib.request.urlopen(
                    request,
                    timeout=15
                ) as response:

                    data = json.loads(
                        response.read().decode("utf-8")
                    )

                json_response(
                    self,
                    200,
                    {
                        "token": data["token"]
                    }
                )

            except Exception as exc:

                json_response(
                    self,
                    500,
                    {
                        "error": (
                            f"Could not create voice token: {exc}"
                        )
                    }
                )

            return


        # ----------------------------------------------------
        # SEND AGENT CONFIGURATION
        # ----------------------------------------------------

        if path == "/api/config":

            json_response(
                self,
                200,
                {
                    "system_prompt": SYSTEM_PROMPT,
                    "greeting": GREETING,
                    "voice": VOICE,
                    "keyterms": KEYTERMS,

                    # BOTH FAN AND LIGHT TOOLS
                    "tools": TOOLS,
                }
            )

            return


        # ----------------------------------------------------
        # WEBSITE FILES
        # ----------------------------------------------------

        if path == "/" or path == "/index.html":

            self.serve_file(
                "index.html",
                "text/html; charset=utf-8"
            )

            return


        if path == "/app.js":

            self.serve_file(
                "app.js",
                "application/javascript; charset=utf-8"
            )

            return


        if path == "/pcm-processor.js":

            self.serve_file(
                "pcm-processor.js",
                "application/javascript; charset=utf-8"
            )

            return


        if path == "/style.css":

            self.serve_file(
                "style.css",
                "text/css; charset=utf-8"
            )

            return


        json_response(
            self,
            404,
            {
                "error": "Not found"
            }
        )


    # --------------------------------------------------------
    # POST REQUESTS
    # --------------------------------------------------------

    def do_POST(self):

        path = urllib.parse.urlparse(
            self.path
        ).path


        # ----------------------------------------------------
        # EXECUTE PYTHON TOOL
        # ----------------------------------------------------

        if path == "/api/tool":

            try:

                content_length = int(
                    self.headers.get(
                        "Content-Length",
                        "0"
                    )
                )

                body = self.rfile.read(
                    content_length
                )

                data = json.loads(
                    body.decode("utf-8")
                )

                tool_name = data.get("name")

                arguments = data.get(
                    "arguments",
                    {}
                )


                # ------------------------------------------------
                # ALLOW BOTH TOOLS
                # ------------------------------------------------

                if tool_name not in (
                    "control_fan",
                    "control_light"
                ):

                    json_response(
                        self,
                        400,
                        {
                            "success": False,
                            "message": (
                                f"Unknown or unauthorized tool: "
                                f"{tool_name}"
                            )
                        }
                    )

                    return


                # ------------------------------------------------
                # EXECUTE TOOL FROM tools.py
                # ------------------------------------------------

                result = dispatch_tool(
                    tool_name,
                    arguments
                )


                print()
                print("========================================")
                print("🔧 PYTHON TOOL EXECUTED")
                print(f"Tool: {tool_name}")
                print(f"Arguments: {arguments}")
                print(f"Result: {result}")
                print("========================================")
                print()


                json_response(
                    self,
                    200,
                    result
                )


            except Exception as exc:

                print(
                    f"[tool] Error: {exc}"
                )

                json_response(
                    self,
                    500,
                    {
                        "success": False,
                        "message": str(exc)
                    }
                )

            return


        json_response(
            self,
            404,
            {
                "error": "Not found"
            }
        )


    # --------------------------------------------------------
    # SERVE WEBSITE FILE
    # --------------------------------------------------------

    def serve_file(
        self,
        name,
        content_type
    ):

        file_path = ROOT / "web" / name


        if not file_path.exists():

            json_response(
                self,
                404,
                {
                    "error": (
                        f"Missing web/{name}"
                    )
                }
            )

            return


        body = file_path.read_bytes()


        self.send_response(200)

        self.send_header(
            "Content-Type",
            content_type
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)


    # --------------------------------------------------------
    # TERMINAL LOG
    # --------------------------------------------------------

    def log_message(
        self,
        format,
        *args
    ):

        print(
            f"[web] {self.address_string()} - "
            f"{format % args}"
        )


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    print()

    print(
        "========================================"
    )

    print(
        "       🎙️ SPEAKEASY WEB SERVER"
    )

    print(
        "========================================"
    )

    print(
        f"Open: http://localhost:{PORT}"
    )

    print(
        "Press Ctrl+C to stop"
    )

    print()


    ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        Handler
    ).serve_forever()