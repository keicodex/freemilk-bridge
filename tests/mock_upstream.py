#!/usr/bin/env python3
"""A tiny echo upstream used by tests/selftest.py (and for manual smoke
tests). It impersonates a FreeMilk-style provider in both wire formats:

  POST /chat/completions   OpenAI-compatible (JSON or SSE when stream=true)
  POST /v1/messages        Anthropic Messages (JSON or Anthropic SSE)
  POST /refresh             returns a rotated apiKey
  GET  /__last             JSON describing the most recent provider request
  GET  /__stats            counters (requests per path, refresh hits)

Auth behaviour: the key "mock-key-stale" is always rejected with 401 (to
exercise the bridge's refresh path); "mock-key-rotated" is what /refresh
hands out; any other key is accepted. Requests are recorded verbatim so
tests can assert on path, headers and body.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    "last": None,
    "counts": {},
}
LOCK = threading.Lock()

ACCEPTED_KEYS = {"mock-key-1", "mock-key-rotated"}


def _record(handler, body):
    with LOCK:
        STATE["last"] = {
            "method": handler.command,
            "path": handler.path,
            "headers": {k.lower(): v for k, v in handler.headers.items()},
            "body": body,
        }
        STATE["counts"][handler.path] = \
            STATE["counts"].get(handler.path, 0) + 1


def _presented_key(handler):
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return handler.headers.get("x-api-key", "")


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, events):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for event in events:
            data = event.encode("utf-8")
            self.wfile.write(b"%X\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/__last":
            with LOCK:
                self._json(200, STATE["last"] or {})
            return
        if self.path == "/__stats":
            with LOCK:
                self._json(200, STATE["counts"])
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {}

        if self.path == "/__reset":
            with LOCK:
                STATE["last"] = None
                STATE["counts"] = {}
            self._json(200, {"ok": True})
            return

        if self.path == "/refresh":
            with LOCK:
                STATE["counts"]["/refresh"] = \
                    STATE["counts"].get("/refresh", 0) + 1
            self._json(200, {"data": {"apiKey": "mock-key-rotated"}})
            return

        _record(self, body)
        key = _presented_key(self)
        if key == "mock-key-stale" or (key and key not in ACCEPTED_KEYS):
            self._json(401, {"error": {"message": "invalid credential"}})
            return

        if self.path.endswith("/chat/completions"):
            self._openai(body)
        elif self.path.endswith("/messages"):
            self._anthropic(body)
        else:
            self._json(404, {"error": "unknown path"})

    def _openai(self, body):
        model = body.get("model", "")
        if body.get("stream"):
            chunks = []
            chunks.append("data: " + json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion.chunk",
                "created": 1700000000, "model": model,
                "choices": [{"index": 0, "delta": {
                    "role": "assistant", "content": ""},
                    "finish_reason": None}],
            }) + "\n\n")
            for piece in ["Hello", " from mock"]:
                chunks.append("data: " + json.dumps({
                    "id": "chatcmpl-mock", "object": "chat.completion.chunk",
                    "created": 1700000000, "model": model,
                    "choices": [{"index": 0, "delta": {"content": piece},
                                 "finish_reason": None}],
                }) + "\n\n")
            chunks.append("data: " + json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion.chunk",
                "created": 1700000000, "model": model,
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": "stop"}],
            }) + "\n\n")
            chunks.append("data: [DONE]\n\n")
            self._sse(chunks)
            return
        self._json(200, {
            "id": "chatcmpl-mock", "object": "chat.completion",
            "created": 1700000000, "model": model,
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": "Hello from mock"},
                "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4,
                      "total_tokens": 7},
        })

    def _anthropic(self, body):
        model = body.get("model", "")
        if body.get("stream"):
            events = [
                'event: message_start\ndata: {"type":"message_start",'
                '"message":{"id":"msg_mock","model":"%s"}}' % model + "\n\n",
                'event: content_block_start\ndata: {"type":'
                '"content_block_start","index":0,"content_block":'
                '{"type":"text","text":""}}' + "\n\n",
                'event: content_block_delta\ndata: {"type":'
                '"content_block_delta","index":0,"delta":{"type":'
                '"text_delta","text":"Hello"}}' + "\n\n",
                'event: content_block_delta\ndata: {"type":'
                '"content_block_delta","index":0,"delta":{"type":'
                '"text_delta","text":" from mock"}}' + "\n\n",
                'event: content_block_stop\ndata: {"type":'
                '"content_block_stop","index":0}' + "\n\n",
                'event: message_delta\ndata: {"type":"message_delta",'
                '"delta":{"stop_reason":"end_turn"}}' + "\n\n",
                'event: message_stop\ndata: {"type":"message_stop"}' + "\n\n",
            ]
            self._sse(events)
            return
        self._json(200, {
            "id": "msg_mock", "type": "message", "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "Hello from mock"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 4},
        })


def create_server(port):
    server = ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18711
    create_server(port).serve_forever()
