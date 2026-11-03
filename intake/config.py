"""Configuration: everything tunable comes from environment variables."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "snapshot.schema.json"

# Bump when office-policy.md routing rules change in a way that affects routing.
POLICY_VERSION = "northstar-policy-2026-10-v1"


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-4.1-mini"))
    base_url: str | None = field(default_factory=lambda: os.environ.get("OPENAI_BASE_URL") or None)
    temperature: float = field(default_factory=lambda: float(_env("INTAKE_TEMPERATURE", "0")))
    max_output_tokens: int = field(default_factory=lambda: int(_env("INTAKE_MAX_OUTPUT_TOKENS", "1500")))
    timeout_s: float = field(default_factory=lambda: float(_env("INTAKE_TIMEOUT_S", "60")))
    max_attempts: int = field(default_factory=lambda: int(_env("INTAKE_MAX_ATTEMPTS", "4")))
    backoff_base_s: float = field(default_factory=lambda: float(_env("INTAKE_BACKOFF_S", "2")))
    workers: int = field(default_factory=lambda: int(_env("INTAKE_WORKERS", "4")))
    lease_s: int = field(default_factory=lambda: int(_env("INTAKE_LEASE_S", "600")))
    db_path: str = field(default_factory=lambda: _env("INTAKE_DB", str(ROOT / "data" / "intake.db")))
    out_dir: str = field(default_factory=lambda: _env("INTAKE_OUT_DIR", str(ROOT / "deliverables")))
    policy_version: str = POLICY_VERSION

    def settings(self) -> dict:
        """Settings that change what the model produces (no secrets)."""
        from .extract import SYSTEM_PROMPT  # local import to avoid a cycle

        return {
            "model": self.model,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:16],
            "schema_sha256": hashlib.sha256(
                json.dumps(load_schema(), sort_keys=True).encode()
            ).hexdigest()[:16],
        }

    def extraction_fp(self) -> str:
        blob = json.dumps(self.settings(), sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]
