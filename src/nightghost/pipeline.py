from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

from nightghost.config import NightGhostConfig
from nightghost.context import ContextBuilder, ProjectContext
from nightghost.executor import Executor, StepExecutionResult
from nightghost.gitmanager import GitManager
from nightghost.guardrail import Guardrail
from nightghost.llm.base import BaseLLMClient
from nightghost.llm.mock import MockLLMClient
from nightghost.llm.ollama import OllamaClient
from nightghost.planner import ExecutionPlan, Planner
from nightghost.report import ReportGenerator
from nightghost.testrunner import TestResult, TestRunner


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


class NightGhostPipeline:
    """The central orchestrator executing the 8-step Night Ghost overnight pipeline."""

    def __init__(
        self,
        repo_root: Path,
        config: Optional[NightGhostConfig] = None,
        llm_client: Optional[BaseLLMClient] = None,
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

        # Select LLM client
        if llm_client:
            self.llm = llm_client
        elif self.config.model.provider == "mock":
            self.llm = MockLLMClient()
        else:
            self.llm = OllamaClient(self.config.model)

    def run(self, dry_run: bool = False, model_override: Optional[str] = None) -> PipelineResult:
        """Execute the complete 8-step pipeline."""
        start_time = time.time()
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        branch_name = f"nightghost/{today_str}"
        original_branch = self.git.get_current_branch()

        if model_override:
            self.config.model.name = model_override

        # ---------------------------------------------------------
        # Step 1: Preflight
        # ---------------------------------------------------------
        if not self.git.is_git_repo():
            return PipelineResult(
                success=False,
                branch_name=original_branch,
                baseline_tests=TestResult(),
                plan=ExecutionPlan(task_note=""),
                error_message=f"Directory '{self.repo_root}' is not a git repository.",
            )

        # Snapshot uncommitted work if repo is dirty
        if self.git.has_uncommitted_changes():
            stash_sha = self.git.create_wip_snapshot()

        # Run baseline test suite
        baseline_res = self.test_runner.run(self.config.commands.test)
        if self.config.safety.require_green_baseline and (baseline_res.failed > 0 or baseline_res.errors > 0):
            return PipelineResult(
                success=False,
                branch_name=original_branch,
                baseline_tests=baseline_res,
                plan=ExecutionPlan(task_note=""),
                error_message=f"Preflight aborted: require_green_baseline=True but baseline failed ({baseline_res.summary}).",
            )

        # ---------------------------------------------------------
        # Step 2: Branch Creation
        # ---------------------------------------------------------
        if not dry_run:
            self.git.create_or_checkout_ghost_branch(branch_name)

        # ---------------------------------------------------------
        # Step 3: Context Building
        # ---------------------------------------------------------
        context_builder = ContextBuilder(self.repo_root, self.config)
        context = context_builder.build_context()

        # ---------------------------------------------------------
        # Step 4: Planning
        # ---------------------------------------------------------
        planner = Planner(self.config, self.llm)
        plan = planner.create_plan(context)

        # Save plan to disk
        plan_file = self.repo_root / ".nightghost" / "plan.json"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

        # ---------------------------------------------------------
        # Step 5: Execution Loop
        # ---------------------------------------------------------
        executor = Executor(
            repo_root=self.repo_root,
            config=self.config,
            llm_client=self.llm,
            guardrail=self.guardrail,
            git_manager=self.git,
            test_runner=self.test_runner,
        )

        step_results = executor.execute_plan(plan, baseline_res, dry_run=dry_run)

        # ---------------------------------------------------------
        # Step 6: Final Verification
        # ---------------------------------------------------------
        final_test_res = self.test_runner.run(self.config.commands.test) if not dry_run else baseline_res

        # ---------------------------------------------------------
        # Step 7: Morning Report Generation
        # ---------------------------------------------------------
        elapsed_seconds = time.time() - start_time
        reporter = ReportGenerator(self.repo_root, self.config)
        report_md = reporter.generate_from_log(
            branch_name=branch_name,
            base_branch=original_branch if original_branch != "HEAD" else "main",
            runtime_seconds=elapsed_seconds,
        )

        # ---------------------------------------------------------
        # Step 8: Exit cleanly and leave branch for human review
        # ---------------------------------------------------------
        return PipelineResult(
            success=True,
            branch_name=branch_name,
            baseline_tests=baseline_res,
            final_tests=final_test_res,
            plan=plan,
            executed_steps=step_results,
            report_markdown=report_md,
            runtime_seconds=elapsed_seconds,
        )
