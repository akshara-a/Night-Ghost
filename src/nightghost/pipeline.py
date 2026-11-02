from __future__ import annotations

import datetime
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from pydantic import BaseModel, Field

from nightghost import state
from nightghost.config import NightGhostConfig
from nightghost.context import ContextBuilder
from nightghost.executor import Executor, StepExecutionResult
from nightghost.gitmanager import GitManager
from nightghost.guardrail import Guardrail
from nightghost.llm.base import BaseLLMClient
from nightghost.llm.mock import MockLLMClient
from nightghost.llm.ollama import OllamaClient
from nightghost.planner import ExecutionPlan, Planner
from nightghost.report import ReportGenerator
from nightghost.testrunner import TestResult, TestRunner

EventCallback = Callable[[str, Dict[str, Any]], None]

# Pipeline outcome states
STATUS_SUCCESS = "success"      # every planned step committed
STATUS_PARTIAL = "partial"      # some steps were reverted or skipped
STATUS_NO_STEPS = "no_steps"    # the planner produced nothing to do
STATUS_ERROR = "error"          # the run could not complete


class PipelineResult(BaseModel):
    success: bool
    branch_name: str
    baseline_tests: TestResult
    final_tests: Optional[TestResult] = None
    plan: ExecutionPlan
    executed_steps: list[StepExecutionResult] = Field(default_factory=list)
    report_markdown: str = ""
    error_message: Optional[str] = None
    runtime_seconds: float = 0.0
    status: str = STATUS_SUCCESS
    base_branch: Optional[str] = None
    branch_existed: bool = False
    wip_snapshot: Optional[str] = None
    wip_restored: Optional[bool] = None

    @property
    def steps_passed(self) -> int:
        return sum(1 for s in self.executed_steps if s.status == "passed")

    @property
    def steps_reverted(self) -> int:
        return sum(1 for s in self.executed_steps if s.status == "reverted")

    @property
    def steps_skipped(self) -> int:
        return sum(1 for s in self.executed_steps if s.status == "skipped")


class NightGhostPipeline:
    """The central orchestrator executing the 8-step Night Ghost overnight pipeline."""

    def __init__(
        self,
        repo_root: Path,
        config: Optional[NightGhostConfig] = None,
        llm_client: Optional[BaseLLMClient] = None,
        on_event: Optional[EventCallback] = None,
    ):
        self.repo_root = repo_root.resolve()
        self.config = config or NightGhostConfig.load(self.repo_root / "nightghost.yaml")
        self.git = GitManager(self.repo_root)
        self.guardrail = Guardrail(self.repo_root, self.config)
        self.test_runner = TestRunner(
            self.repo_root,
            timeout_seconds=self.config.commands.timeout_seconds,
            network=self.config.safety.network,
        )
        self.on_event = on_event
        self._llm_injected = llm_client is not None
        self.llm = llm_client or self._make_llm()

    def _make_llm(self) -> BaseLLMClient:
        if self.config.model.provider == "mock":
            return MockLLMClient()
        return OllamaClient(self.config.model)

    def _emit(self, event: str, **data: Any) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event, data)
            except Exception:  # noqa: BLE001 - a UI listener must never break the pipeline
                pass

    def _failure(
        self,
        branch: str,
        message: str,
        baseline: Optional[TestResult] = None,
        start_time: Optional[float] = None,
    ) -> PipelineResult:
        return PipelineResult(
            success=False,
            status=STATUS_ERROR,
            branch_name=branch,
            baseline_tests=baseline or TestResult(),
            plan=ExecutionPlan(task_note=""),
            error_message=message,
            runtime_seconds=(time.time() - start_time) if start_time else 0.0,
        )

    def run(self, dry_run: bool = False, model_override: Optional[str] = None) -> PipelineResult:
        """Execute the complete 8-step pipeline.

        The caller's branch is always restored afterwards (even on a crash) and any uncommitted
        work that was snapshotted is re-applied, so the user's working tree is left as found.
        """
        start_time = time.time()

        # Apply the model override on a private copy: never mutate the caller's config.
        if model_override:
            self.config = self.config.model_copy(deep=True)
            self.config.model.name = model_override
            if not self._llm_injected:
                self.llm = self._make_llm()

        # Preflight: repository and kill switch
        if not self.git.is_git_repo():
            return self._failure(
                "HEAD", f"Directory '{self.repo_root}' is not a git repository.", start_time=start_time
            )

        original_branch = self.git.get_current_branch()
        original_ref = original_branch if original_branch != "HEAD" else (self.git.get_head_sha() or "HEAD")
        if original_branch.startswith("nightghost/") or original_branch == "HEAD":
            base_branch = self.git.default_branch()
        else:
            base_branch = original_branch

        if state.stop_file(self.repo_root).exists():
            return self._failure(
                original_branch,
                "Kill switch is engaged (.nightghost/STOP). Clear it with `nightghost resume`.",
                start_time=start_time,
            )

        today_str = datetime.date.today().strftime("%Y-%m-%d")
        branch_name = f"nightghost/{today_str}"

        stash_sha: Optional[str] = None
        switched = False
        branch_existed = False
        result: PipelineResult
        try:
            # Step 1: Preflight - snapshot uncommitted work, then run the baseline test suite
            self._emit("PHASE", message="Preflight: running baseline tests")
            if self.git.has_uncommitted_changes():
                stash_sha = self.git.create_wip_snapshot()

            baseline_res = self.test_runner.run(self.config.commands.test)
            if self.config.safety.require_green_baseline and (
                baseline_res.failed > 0 or baseline_res.errors > 0
            ):
                return self._failure(
                    original_branch,
                    "Preflight aborted: require_green_baseline=True but baseline failed "
                    f"({baseline_res.summary}).",
                    baseline=baseline_res,
                    start_time=start_time,
                )

            # Step 2: Branch creation
            if not dry_run:
                branch_existed = self.git.branch_exists(branch_name)
                self._emit(
                    "PHASE",
                    message=(
                        f"Continuing existing branch {branch_name}"
                        if branch_existed
                        else f"Creating branch {branch_name}"
                    ),
                )
                self.git.create_or_checkout_ghost_branch(branch_name)
                switched = True

            # Step 3: Context building
            self._emit("PHASE", message="Reading your note and recent changes")
            context = ContextBuilder(self.repo_root, self.config).build_context()

            # Step 4: Planning
            self._emit("PHASE", message=f"Planning with {self.config.model.name}")
            plan = Planner(self.config, self.llm).create_plan(context)

            plan_file = self.repo_root / ".nightghost" / "plan.json"
            plan_file.parent.mkdir(parents=True, exist_ok=True)
            plan_file.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
            self._emit("PLAN_READY", total_steps=len(plan.steps))

            # Step 5: Execution loop
            executor = Executor(
                repo_root=self.repo_root,
                config=self.config,
                llm_client=self.llm,
                guardrail=self.guardrail,
                git_manager=self.git,
                test_runner=self.test_runner,
                on_event=self.on_event,
            )
            step_results = executor.execute_plan(plan, baseline_res, dry_run=dry_run)

            # Step 6: Final verification
            if dry_run:
                final_test_res = baseline_res
            else:
                self._emit("PHASE", message="Final verification: running test suite")
                final_test_res = self.test_runner.run(self.config.commands.test)

            # Step 7: Morning report
            self._emit("PHASE", message="Writing morning report")
            elapsed_seconds = time.time() - start_time
            report_md = ReportGenerator(self.repo_root, self.config).generate_from_log(
                branch_name=branch_name,
                base_branch=base_branch,
                runtime_seconds=elapsed_seconds,
            )

            if not plan.steps:
                status = STATUS_NO_STEPS
            elif all(s.status == "passed" for s in step_results) and len(step_results) == len(plan.steps):
                status = STATUS_SUCCESS
            else:
                status = STATUS_PARTIAL

            result = PipelineResult(
                success=True,
                status=status,
                branch_name=branch_name,
                base_branch=base_branch,
                branch_existed=branch_existed,
                baseline_tests=baseline_res,
                final_tests=final_test_res,
                plan=plan,
                executed_steps=step_results,
                report_markdown=report_md,
                runtime_seconds=elapsed_seconds,
                wip_snapshot=stash_sha,
            )
        except Exception as e:  # noqa: BLE001 - a crash must still restore the user's branch
            result = self._failure(
                branch_name if switched else original_branch,
                f"Unexpected error: {type(e).__name__}: {e}",
                start_time=start_time,
            )
            result.wip_snapshot = stash_sha
        finally:
            # Step 8: leave the repo as we found it; the ghost branch stays for human review.
            if switched:
                try:
                    self.git.checkout(original_ref)
                except Exception:  # noqa: BLE001
                    pass

        if switched and stash_sha:
            result.wip_restored = self.git.apply_snapshot(stash_sha)

        result.runtime_seconds = result.runtime_seconds or (time.time() - start_time)
        if not dry_run and result.success:
            state.save_last_run(
                self.repo_root,
                branch=branch_name,
                base_branch=base_branch,
                date=today_str,
                status=result.status,
                wip_snapshot=stash_sha,
                steps_planned=len(result.plan.steps),
                steps_passed=result.steps_passed,
                runtime_seconds=round(result.runtime_seconds, 1),
            )
        return result
