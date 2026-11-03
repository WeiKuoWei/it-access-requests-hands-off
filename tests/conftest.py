import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx2 as httpx
import openai
import pytest

from intake.config import ROOT, Config
from intake.store import Store

REQUESTS = ROOT / "materials" / "requests"


def completion(body, finish="stop", refusal=None):
    content = body if isinstance(body, str) or body is None else json.dumps(body)
    msg = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(id="resp_test", choices=[SimpleNamespace(message=msg, finish_reason=finish)])


def record(**over):
    """A schema-valid extraction; tests override fields. Values are written by hand, not model output."""
    base = dict(request_id="IT-001", request_date="2026-10-22", kind="new", requester="Maya Chen",
                team="Analytics", system="Atlas", environment="staging", permission="read",
                business_purpose="check totals", start_date="2026-10-23", end_date="2026-11-06",
                manager="Priya Shah", supporting_quotes=[], issues=[])
    base.update(over)
    return base


def api_error(cls, status):
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    if cls is openai.APITimeoutError:
        return cls(req)
    if cls is openai.APIConnectionError:
        return cls(request=req)
    return cls("boom", response=httpx.Response(status, request=req), body=None)


class FakeClient:
    """Scripted provider. `script` is a callable(message, call_no) -> response or exception."""

    def __init__(self, script):
        self.script, self.calls, self.messages = script, 0, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **req):
        text = req["messages"][-1]["content"]
        message = text.removeprefix("<message>\n").removesuffix("\n</message>")
        self.calls += 1
        self.messages.append(message)
        out = self.script(message, self.calls)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=str(tmp_path / "t.db"), out_dir=str(tmp_path / "out"), backoff_base_s=0,
                  max_attempts=2, workers=3)


@pytest.fixture
def store(cfg):
    return Store(cfg.db_path)


@pytest.fixture
def inbox(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    for p in REQUESTS.glob("*.json"):
        shutil.copy(p, d / p.name)
    return d


def no_sleep(_):
    return None
