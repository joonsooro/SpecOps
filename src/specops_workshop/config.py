from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


GEMINI_MODEL = "gemini-3.1-flash-live-preview"
TERRA_MODEL = "gpt-5.6-terra"
AnalyzerReasoningEffort = Literal["medium"]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    specops_database_url: str = Field(min_length=1)
    workshop_database_url: str = Field(min_length=1)
    gemini_api_key: SecretStr = Field(repr=False)
    openai_api_key: SecretStr = Field(repr=False)
    gemini_model: str = GEMINI_MODEL
    terra_model: str = TERRA_MODEL
    analyzer_reasoning_effort: AnalyzerReasoningEffort = "medium"

    @classmethod
    def load(
        cls,
        environ: Mapping[str, str],
        env_path: Path | None = None,
    ) -> Settings:
        values = _read_env(env_path) if env_path is not None else {}
        values.update(environ)
        banned = sorted(key for key in values if key.upper().startswith(("JIRA_", "GITHUB_")))
        if banned:
            raise ValueError("Jira/GitHub configuration is outside the Workshop boundary")
        mapped = {
            "specops_database_url": values.get("SPECOPS_DATABASE_URL"),
            "workshop_database_url": values.get("WORKSHOP_DATABASE_URL"),
            "gemini_api_key": values.get("GEMINI_API_KEY"),
            "openai_api_key": values.get("OPENAI_API_KEY"),
            "gemini_model": values.get("GEMINI_LIVE_MODEL", GEMINI_MODEL),
            "terra_model": values.get("OPENAI_ANALYZER_MODEL", TERRA_MODEL),
            "analyzer_reasoning_effort": values.get(
                "OPENAI_ANALYZER_REASONING_EFFORT", "medium"
            ),
        }
        missing = sorted(key for key, value in mapped.items() if value is None)
        if missing:
            raise ValueError(f"missing required Workshop settings: {', '.join(missing)}")
        settings = cls.model_validate(mapped)
        if settings.gemini_model != GEMINI_MODEL or settings.terra_model != TERRA_MODEL:
            raise ValueError("Workshop provider models must match the pinned release models")
        return settings


def _read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError("Workshop environment file is missing")
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid environment line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in values:
            raise ValueError(f"invalid environment key on line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values
