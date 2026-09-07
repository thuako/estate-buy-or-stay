from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = "thuako/estate-buy-or-stay"
    research_as_of: str = "2026-09-07"
    model: str | None = None
    max_parallel: int = Field(default=3, ge=1, le=3)
    max_agent_calls: int = Field(default=24, ge=1, le=100)
    max_tool_calls: int = Field(default=60, ge=1)
    max_wall_seconds: int = Field(default=1800, ge=1)
    task_timeout_seconds: int = Field(default=300, ge=1)
    max_attempts: int = Field(default=3, ge=1, le=3)
    max_discussion_rounds: int = Field(default=2, ge=0, le=2)
    poll_seconds: int = Field(default=20, ge=5, le=60)
    allowed_github_users: list[str] = Field(default_factory=list)
    github_logins: dict[str, str] = Field(default_factory=dict)

    @field_validator("repository")
    @classmethod
    def valid_repo(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
            raise ValueError("repository must be owner/name")
        return value


def read_config(path: Path) -> Config:
    return Config.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
