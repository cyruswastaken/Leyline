"""A tiny OpenAI-compatible chat server for load tests.

It streams a canned, citation-bearing answer at a steady pace, so a load test
measures Leyline (search, database, streaming, persistence) rather than how fast
one local GPU can generate tokens.

    python loadtest/fake_llm.py            # listens on 0.0.0.0:9099
    FAKE_LLM_DELAY_MS=40 FAKE_LLM_PORT=9099 python loadtest/fake_llm.py
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY = float(os.environ.get("FAKE_LLM_DELAY_MS", "30")) / 1000
PORT = int(os.environ.get("FAKE_LLM_PORT", "9099"))
WORDS = (  # noqa: SIM905
    "Open the approved client, sign in with your corporate account and confirm "
    "the connection status [1]. If a code is requested, use your authenticator "
    "app [1]. Contact the helpdesk if the problem continues."
).split()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for word in WORDS:
                time.sleep(DELAY)
                chunk = {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fake",
                    "choices": [{"index": 0, "delta": {"content": word + " "}}],
                }
                self._chunk(f"data: {json.dumps(chunk)}\n\n")
            self._chunk("data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        # Non-streaming calls are query rewrites: echo the last question.
        text = body["messages"][-1]["content"].rsplit("\n", 1)[-1][:200]
        payload = json.dumps(
            {
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "fake",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _chunk(self, text: str) -> None:
        data = text.encode()
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)  # noqa: S104
    server.daemon_threads = True
    print(f"fake LLM on :{PORT}, {DELAY * 1000:.0f} ms per word", flush=True)
    server.serve_forever()
