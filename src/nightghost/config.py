from __future__ import annotations

from pathlib import Path
from typing import List
import yaml
from pydantic import BaseModel, Field, field_validator


class ModelConfig(BaseModel):
    provider: str = Field(default="ollama", description="LLM provider: ollama, llamacpp, or mock")
    name: str = Field(default="gemma4:e2b", description="Model name")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    context_window: int = Field(default=8192, gt=0)
    api_base: str = Field(default="http://localhost:11434", description="Base URL for LLM API")


class TaskConfig(BaseModel):
    file: str = Field(default=".nightghost/task.md", description="Path to task note markdown")


class ScopeConfig(BaseModel):
    allow: List[str] = Field(
        default_factory=lambda: ["src/**", "tests/**"],
        description="Glob patterns of files allowed to be modified",
    )
    deny: List[str] = Field(
        default_factory=lambda: [
            ".env*",
            "migrations/**",
            "**/*.pem",
            "**/*.key",
            ".github/**",
            "*.lock",
            ".git/**",
            "nightghost.yaml",
        ],
        description="Glob patterns of files strictly forbidden from modification",
    )


class CommandsConfig(BaseModel):
    test: str = Field(default="pytest -q", description="Command to execute the test suite")
    lint: str = Field(default="ruff check .", description="Command for linting")
    format: str = Field(default="ruff format .", description="Command for formatting")
    timeout_seconds: int = Field(default=300, gt=0, description="Max execution time per command")


class LimitsConfig(BaseModel):
    max_steps: int = Field(default=8, ge=1, le=50)
    max_files_changed: int = Field(default=15, ge=1)
    max_lines_changed: int = Field(default=800, ge=1)
    max_runtime_minutes: int = Field(default=120, ge=1)
    max_retries_per_step: int = Field(default=2, ge=0)


class SafetyConfig(BaseModel):
    network: str = Field(default="off", description="Network access policy: 'off' or 'restricted'")
    never_edit_existing_assertions: bool = Field(
        default=True,
        description="Hard rule: prevent modifying existing test assertions to make tests pass",
    )
    require_green_baseline: bool = Field(
        default=False,
        description="If True, baseline test suite must pass 100% before starting. If False, allows no regressions.",
    )


class ScheduleConfig(BaseModel):
    at: str = Field(default="01:00", description="Time of night to run (HH:MM 24h format)")

    @field_validator("at")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError("schedule.at must look like HH:MM (24h), e.g. 01:30")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("schedule.at must be a valid 24h time, e.g. 01:30")
        return f"{hour:02d}:{minute:02d}"


class NightGhostConfig(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    task: TaskConfig = Field(default_factory=TaskConfig)
    scope: ScopeConfig = Field(default_factory=ScopeConfig)
    commands: CommandsConfig = Field(default_factory=CommandsConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)

    @classmethod
    def load(cls, path: Path | str = "nightghost.yaml") -> "NightGhostConfig":
        config_path = Path(path)
        if not config_path.is_file():
            return cls()

        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        return cls.model_validate(data)

    def save(self, path: Path | str = "nightghost.yaml") -> None:
        config_path = Path(path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump()
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)


DEFAULT_YAML_TEMPLATE = """# Night Ghost Configuration
# See https://github.com/nightghost/nightghost for documentation.

model:
  provider: ollama            # or llamacpp / mock
  name: gemma4:e2b
  temperature: 0.2
  context_window: 8192
  api_base: http://localhost:11434

task:
  file: .nightghost/task.md

scope:
  allow:
    - "src/**"
    - "tests/**"
  deny:
    - ".env*"
    - "migrations/**"
    - "**/*.pem"
    - "**/*.key"
    - ".github/**"
    - "*.lock"
    - ".git/**"
    - "nightghost.yaml"

commands:
  test: "pytest -q"
  lint: "ruff check ."
  format: "ruff format ."
  timeout_seconds: 300

limits:
  max_steps: 8
  max_files_changed: 15
  max_lines_changed: 800
  max_runtime_minutes: 120
  max_retries_per_step: 2

safety:
  network: "off"
  never_edit_existing_assertions: true
  require_green_baseline: false

schedule:
  at: "01:00"
"""
