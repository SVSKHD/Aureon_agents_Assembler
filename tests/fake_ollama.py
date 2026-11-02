"""A stand-in Ollama HTTP server (stdlib only) for the local acceptance run (docs/24).

    python tests/fake_ollama.py --port 11999

GET /api/tags, /api/ps, /api/version; POST /api/chat streams NDJSON like Ollama. It never
runs a model: it answers "ok". Not used by production code.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = {
    "name": "fake-model:latest",
    "model": "fake-model:latest",
    "digest": "0" * 64,
    "size": 1,
    "modified_at": "2026-11-02T00:00:00Z",
    "details": {"family": "fake", "parameter_size": "0B", "quantization_level": "none"},
}


class Handler(BaseHTTPRequestHandler):
    def _json(self, body: dict[str, object]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/api/tags":
            self._json({"models": [MODEL]})
        elif self.path == "/api/ps":
            self._json({"models": []})
        elif self.path == "/api/version":
            self._json({"version": "0.0.0-fake"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        if self.path != "/api/chat" or req.get("model") != MODEL["name"]:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"model not found"}')
            return
        self.send_response(200)
        self.send_header("content-type", "application/x-ndjson")
        self.end_headers()
        for word in ("o", "k"):
            line = {
                "model": MODEL["name"],
                "message": {"role": "assistant", "content": word},
                "done": False,
            }
            self.wfile.write((json.dumps(line) + "\n").encode())
            self.wfile.flush()
        done = {
            "model": MODEL["name"],
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "eval_count": 2,
            "prompt_eval_count": 5,
        }
        self.wfile.write((json.dumps(done) + "\n").encode())

    def log_message(self, *args: object) -> None:
        return


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11999)
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
