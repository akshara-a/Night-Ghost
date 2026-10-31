from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from nightghost.config import NightGhostConfig
from nightghost.gitmanager import GitManager
from nightghost.guardrail import Guardrail, GuardrailResult
from nightghost.llm.base import BaseLLMClient, LLMMessage, ToolCall
from nightghost.planner import ExecutionPlan, PlannedStep
from nightghost.testrunner import TestResult, TestRunner
from nightghost.tools import ToolExecutionResult, ToolRegistry


class ActionLogEntry(BaseModel):
    timestamp: str = Field(default_factory=lambda: datetime.datetime.now().isoformat())
    event: str
    step_id: Optional[int] = None
    step_title: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


class JudgmentItem(BaseModel):
    title: str
    description: str
    file_path: Optional[str] = None


class StepExecutionResult(BaseModel):
    step_id: int
    title: str
    status: str  # "passed", "reverted", "skipped"
    commit_sha: Optional[str] = None
    retries_used: int = 0
    failure_reason: Optional[str] = None
    test_result: Optional[TestResult] = None


class Executor:
    """Executes planned steps with tool-calling loops, guardrails, test gates, and atomic commits."""

    def __init__(
        self,
        repo_root: Path,
        config: NightGhostConfig,
        llm_client: BaseLLMClient,
        guardrail: Guardrail,
        git_manager: GitManager,
        test_runner: TestRunner,
        log_dir: Optional[Path] = None,
    ):
        self.repo_root = repo_root.resolve()
        self.config = config
        self.llm = llm_client
        self.guardrail = guardrail
        self.git = git_manager
        self.test_runner = test_runner
        self.log_dir = log_dir or (self.repo_root / ".nightghost")
        self.log_file = self.log_dir / "actions.jsonl"
        self.tools = ToolRegistry(self.repo_root, guardrail, test_runner, git_manager)

        self.action_logs: List[ActionLogEntry] = []
        self.judgment_items: List[JudgmentItem] = []
        self.denials: List[Dict[str, Any]] = []

    def log(
        self,
        event: str,
        step_id: Optional[int] = None,
        step_title: Optional[str] = None,
        **kwargs: Any,
    ) -> ActionLogEntry:
        """Record an event in the structured JSONL action log."""
        entry = ActionLogEntry(
            event=event,
            step_id=step_id,
            step_title=step_title,
            data=kwargs,
        )
        self.action_logs.append(entry)

        self.log_dir.mkdir(parents=True, exist_ok=True)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")

        return entry

    def execute_plan(
        self,
        plan: ExecutionPlan,
        baseline_test: TestResult,
        dry_run: bool = False,
    ) -> List[StepExecutionResult]:
        """Execute each step in the plan sequentially with safety checks and rollback on regression."""
        step_results: List[StepExecutionResult] = []
        start_time = time.time()
        max_seconds = self.config.limits.max_runtime_minutes * 60

        self.log(
            "PLAN_START",
            total_steps=len(plan.steps),
            baseline_summary=baseline_test.summary,
            baseline_passed=baseline_test.passed,
            baseline_failed=baseline_test.failed,
            dry_run=dry_run,
        )

        for step in plan.steps:
            # 1. Check time budget
            if time.time() - start_time > max_seconds:
                self.log(
                    "RUNTIME_LIMIT_EXCEEDED",
                    step_id=step.id,
                    step_title=step.title,
                    limit_minutes=self.config.limits.max_runtime_minutes,
                )
                step_results.append(
                    StepExecutionResult(
                        step_id=step.id,
                        title=step.title,
                        status="skipped",
                        failure_reason="Overnight runtime budget exceeded",
                    )
                )
                continue

            # 2. Check kill switch
            kill_check = self.guardrail.check_kill_switch()
            if not kill_check.allowed:
                self.log(
                    "KILL_SWITCH_TRIGGERED",
                    step_id=step.id,
                    step_title=step.title,
                    reason=kill_check.reason,
                )
                step_results.append(
                    StepExecutionResult(
                        step_id=step.id,
                        title=step.title,
                        status="skipped",
                        failure_reason=kill_check.reason,
                    )
                )
                break

            if dry_run:
                self.log("DRY_RUN_STEP", step_id=step.id, step_title=step.title)
                step_results.append(
                    StepExecutionResult(
                        step_id=step.id,
                        title=step.title,
                        status="passed",
                        commit_sha="dry-run",
                    )
                )
                continue

            # 3. Execute step with retries
            step_res = self._execute_single_step(step, baseline_test)
            step_results.append(step_res)

        # Find final test result from steps or fallback to baseline
        final_test = baseline_test
        for step_res in reversed(step_results):
            if step_res.test_result:
                final_test = step_res.test_result
                break

        self.log(
            "PLAN_FINISH",
            completed_steps=len(step_results),
            final_passed=final_test.passed,
            final_failed=final_test.failed,
            final_summary=final_test.summary,
            dry_run=dry_run,
        )
        return step_results

    def _execute_single_step(
        self,
        step: PlannedStep,
        baseline_test: TestResult,
    ) -> StepExecutionResult:
        """Execute a single planned step with tool-calling loops and retry logic."""
        self.log("STEP_START", step_id=step.id, step_title=step.title, target_files=step.target_files)

        max_retries = self.config.limits.max_retries_per_step
        tool_defs = self.tools.get_definitions()

        messages = [
            LLMMessage(
                role="system",
                content=(
                    f"You are Night Ghost executing step #{step.id}: '{step.title}'.\n"
                    f"Goal: {step.description}\n"
                    f"Target Files: {step.target_files}\n"
                    f"Verification Command: {step.verification_command}\n\n"
                    "RULES:\n"
                    "1. Always read files before editing.\n"
                    "2. Propose behavior-preserving edits and tests.\n"
                    "3. Do not modify existing test assertions.\n"
                    "4. If you spot potential bugs or dead code that shouldn't be altered without human judgment, "
                    "flag it in your explanation starting with '[NEEDS JUDGMENT]: <description>'.\n"
                    "5. Conclude your turn when changes are written and verified."
                ),
            ),
            LLMMessage(
                role="user",
                content=f"Please execute step #{step.id} for target files: {step.target_files}",
            ),
        ]

        retries_used = 0
        last_failure_reason = ""
        last_test_result: Optional[TestResult] = None

        while retries_used <= max_retries:
            # LLM tool loop for this attempt (up to 5 tool turns)
            for turn in range(5):
                resp = self.llm.generate(messages, tools=tool_defs)

                if resp.content:
                    self._check_for_judgment_flags(resp.content, step.id)

                if not resp.tool_calls:
                    # Model finished proposing tool actions
                    break

                # Execute tool calls
                for tc in resp.tool_calls:
                    exec_result = self.tools.execute(tc)
                    
                    # Log tool and guardrail outcomes
                    if exec_result.guardrail_result and not exec_result.guardrail_result.allowed:
                        self.log(
                            "GUARDRAIL_DENIAL",
                            step_id=step.id,
                            step_title=step.title,
                            tool=tc.name,
                            category=exec_result.guardrail_result.category,
                            reason=exec_result.guardrail_result.reason,
                            details=exec_result.guardrail_result.details,
                        )
                        self.denials.append(exec_result.guardrail_result.details or {"reason": exec_result.guardrail_result.reason})
                    else:
                        self.log(
                            "TOOL_CALL",
                            step_id=step.id,
                            step_title=step.title,
                            tool=tc.name,
                            success=exec_result.success,
                        )

                    # Feed tool result back to model
                    messages.append(
                        LLMMessage(
                            role="tool",
                            name=tc.name,
                            tool_call_id=tc.id,
                            content=exec_result.output[:4000],  # cap output size in context
                        )
                    )

            # Test-gate verification
            test_res = self.test_runner.run(step.verification_command)
            last_test_result = test_res

            regressed, reg_reason = self.test_runner.check_regression(
                baseline_test, test_res, self.config.safety.require_green_baseline
            )

            if not regressed and test_res.exit_code == 0:
                # Tests pass and no regression! Commit the step!
                commit_sha = self.git.commit_step(step.title)
                self.log(
                    "STEP_COMMIT",
                    step_id=step.id,
                    step_title=step.title,
                    commit=commit_sha,
                    test_summary=test_res.summary,
                )
                return StepExecutionResult(
                    step_id=step.id,
                    title=step.title,
                    status="passed",
                    commit_sha=commit_sha,
                    retries_used=retries_used,
                    test_result=test_res,
                )

            # Failure detected
            last_failure_reason = reg_reason
            retries_used += 1

            if retries_used <= max_retries:
                self.log(
                    "STEP_RETRY",
                    step_id=step.id,
                    step_title=step.title,
                    attempt=retries_used,
                    reason=reg_reason,
                )
                # Revert working directory to try again cleanly
                self.git.revert_working_directory()
                messages.append(
                    LLMMessage(
                        role="user",
                        content=(
                            f"Verification failed on attempt {retries_used}: {reg_reason}\n"
                            "Changes were reset. Please adjust your approach to ensure tests pass."
                        ),
                    )
                )
            else:
                # Retries exhausted -> Revert step
                self.git.revert_working_directory()
                self.log(
                    "STEP_REVERT",
                    step_id=step.id,
                    step_title=step.title,
                    retries=retries_used - 1,
                    reason=last_failure_reason,
                )
                return StepExecutionResult(
                    step_id=step.id,
                    title=step.title,
                    status="reverted",
                    retries_used=retries_used - 1,
                    failure_reason=last_failure_reason,
                    test_result=last_test_result,
                )

        return StepExecutionResult(
            step_id=step.id,
            title=step.title,
            status="reverted",
            failure_reason=last_failure_reason,
            test_result=last_test_result,
        )

    def _check_for_judgment_flags(self, text: str, step_id: int) -> None:
        """Extract human judgment flags from LLM outputs."""
        for line in text.splitlines():
            if "[NEEDS JUDGMENT]" in line.upper():
                clean_line = line.replace("[NEEDS JUDGMENT]:", "").replace("[NEEDS JUDGMENT]", "").strip()
                self.judgment_items.append(
                    JudgmentItem(
                        title=f"Flagged in step #{step_id}",
                        description=clean_line,
                    )
                )
                self.log("JUDGMENT_FLAG", step_id=step_id, note=clean_line)
