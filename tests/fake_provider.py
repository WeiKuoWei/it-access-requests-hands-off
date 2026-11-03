"""A tiny OpenAI-compatible HTTP server for multi-process tests.

Operators run as real OS processes using the real `openai` client over real HTTP (OPENAI_BASE_URL), so the
only thing faked is the model. Every call is counted per message so tests can prove no work was duplicated.
Request text is expected to look like "request number N" (see make_inbox in test_scale.py).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def record_for(message: str) -> dict:
    n = int(message.split()[-1])
    return dict(request_id=f"IT-{n:05d}", request_date="2026-10-22", kind="new", requester="Test User",
                team="Analytics", system="Atlas", environment="staging", permission="read",
                business_purpose="testing", start_date="2026-10-23", end_date="2026-10-30",
                manager="Test Manager", supporting_quotes=[], issues=[])


class FakeProvider:
    def __init__(self, latency_s: float = 0.0):
        self.latency_s = latency_s
        self.fail_status: int | None = None
        self.calls: dict[str, int] = {}
        self.models: dict[str, set] = {}
        self._lock = threading.Lock()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep test output quiet
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                text = body["messages"][-1]["content"]
                message = text.removeprefix("<message>\n").removesuffix("\n</message>")
                with provider._lock:
                    provider.calls[message] = provider.calls.get(message, 0) + 1
                    provider.models.setdefault(message, set()).add(body["model"])
                time.sleep(provider.latency_s)
                if provider.fail_status:
                    payload = json.dumps({"error": {"message": "provider unavailable", "type": "server_error"}})
                    self._send(provider.fail_status, payload)
                    return
                resp = {"id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": body["model"],
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "refusal": None,
                                                 "content": json.dumps(record_for(message))}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                self._send(200, json.dumps(resp))

            def _send(self, status, payload):
                data = payload.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def total_calls(self) -> int:
        with self._lock:
            return sum(self.calls.values())

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.calls)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
