from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List
from pydantic import BaseModel, Field

from nightghost.config import NightGhostConfig
from nightghost.context import ProjectContext
from nightghost.llm.base import BaseLLMClient, LLMMessage


class PlannedStep(BaseModel):
    id: int
    title: str
    description: str
    target_files: List[str] = Field(default_factory=list)
    verification_command: str = "pytest -q"
    kind: str = "test"  # "test", "refactor", "cleanup"


class ExecutionPlan(BaseModel):
    task_note: str
    steps: List[PlannedStep] = Field(default_factory=list)
    created_at: str = ""


PLANNER_SYSTEM_PROMPT = """You are Night Ghost's autonomous Planner.
Your job is to read the developer's unfinished task note and repository context, and break it down into small, ordered, safe steps for an overnight run.

CORE RULES:
1. Every step must be small, discrete, and verifiable.
2. Safety first: Step 1 should always add or reinforce tests before touching implementation code.
3. Refactoring or cleanups MUST be behavior-preserving only.
4. Target files must stay strictly within allowed files (e.g. src/**, tests/**).
5. Never propose more than {max_steps} steps.

Output ONLY a JSON array of step objects with this schema:
[
  {{
    "id": 1,
    "title": "Short title describing the step",
    "description": "Clear instructions for what tests or safe edits to write",
    "target_files": ["tests/test_foo.py", "src/foo.py"],
    "verification_command": "{test_cmd}",
    "kind": "test"
  }}
]
"""


class Planner:
    """Generates a structured, verification-gated execution plan from context and task note."""

    def __init__(self, config: NightGhostConfig, llm_client: BaseLLMClient):
        self.config = config
        self.llm = llm_client

    def create_plan(self, context: ProjectContext) -> ExecutionPlan:
        """Create structured execution plan from context."""
        task_text = context.task_note.strip()
        if not task_text:
            task_text = "Add missing unit tests and perform safe code cleanups."

        # Prepare summary of repo
        repo_files = list(context.repo_map.keys())
        symbols_summary = []
        for path, summary in list(context.repo_map.items())[:10]:
            sym_names = [f"{s.kind} {s.name}" for s in summary.symbols[:5]]
            symbols_summary.append(f"- {path}: {', '.join(sym_names) if sym_names else 'empty'}")

        user_content = (
            f"Developer's Task Note:\n\"{task_text}\"\n\n"
            f"Repository Files in Scope:\n{json.dumps(repo_files, indent=2)}\n\n"
            f"Key Symbols:\n{chr(10).join(symbols_summary)}\n\n"
            f"TODO markers found:\n{json.dumps(context.todos[:5], indent=2)}\n\n"
            f"Recent Commits:\n{context.recent_commits}\n\n"
            f"Configured Test Command: {self.config.commands.test}\n\n"
            f"Please generate the ordered JSON plan (max {self.config.limits.max_steps} steps)."
        )

        messages = [
            LLMMessage(
                role="system",
                content=PLANNER_SYSTEM_PROMPT.format(
                    max_steps=self.config.limits.max_steps,
                    test_cmd=self.config.commands.test,
                ),
            ),
            LLMMessage(role="user", content=user_content),
        ]

        resp = self.llm.generate(messages, json_mode=True)
        steps = self._parse_steps(resp.content, context)

        # Ensure steps do not exceed limit
        steps = steps[: self.config.limits.max_steps]

        return ExecutionPlan(
            task_note=task_text,
            steps=steps,
        )

    def _parse_steps(self, text: str, context: ProjectContext) -> List[PlannedStep]:
        """Parse JSON response from LLM or generate sensible fallback steps."""
        # Try extracting JSON array
        json_match = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
        if json_match:
            try:
                raw_list = json.loads(json_match.group(0))
                steps = []
                for idx, item in enumerate(raw_list, start=1):
                    steps.append(
                        PlannedStep(
                            id=idx,
                            title=item.get("title", f"Step {idx}"),
                            description=item.get("description", ""),
                            target_files=item.get("target_files", []),
                            verification_command=item.get(
                                "verification_command", self.config.commands.test
                            ),
                            kind=item.get("kind", "test"),
                        )
                    )
                if steps:
                    return steps
            except Exception:
                pass

        # Fallback heuristic plan if LLM output could not be parsed
        source_targets = context.source_files[:2]
        test_targets = context.test_files[:2]
        if not test_targets and source_targets:
            test_targets = [f"tests/test_{Path(f).stem}.py" for f in source_targets]

        return [
            PlannedStep(
                id=1,
                title="Add unit tests for primary module edge cases",
                description="Write comprehensive unit tests covering happy paths and error cases.",
                target_files=test_targets or ["tests/test_main.py"],
                verification_command=self.config.commands.test,
                kind="test",
            ),
            PlannedStep(
                id=2,
                title="Safe cleanups and format checks",
                description="Fix syntax style and formatting without altering behavior.",
                target_files=source_targets or ["src/main.py"],
                verification_command=self.config.commands.test,
                kind="cleanup",
            ),
        ]
