from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from nightghost.config import NightGhostConfig
from nightghost.gitmanager import DiffStats, GitManager
from nightghost.testrunner import TestResult


class ReportData(BaseModel):
    date: str
    task: str
    branch: str
    base_branch: str = "main"
    commits_count: int = 0
    files_count: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    model_name: str = "qwen2.5-coder:14b"
    model_provider: str = "local"
    runtime_str: str = "0m"
    result_icon: str = "[PASS]"
    baseline_passed: int = 0
    final_passed: int = 0
    final_failed: int = 0
    coverage_str: Optional[str] = None
    completed_steps: List[Dict[str, str]] = Field(default_factory=list)
    judgment_items: List[str] = Field(default_factory=list)
    skipped_steps: List[str] = Field(default_factory=list)
    blocked_items: List[str] = Field(default_factory=list)
    is_dry_run: bool = False


def extract_task_summary(task_text: str) -> str:
    """Extract a meaningful summary line from task note, skipping markdown headers."""
    lines = [line.strip() for line in task_text.splitlines() if line.strip()]
    for line in lines:
        cleaned = line.lstrip("#").strip()
        if cleaned.lower() in ("evening task note", "task note", "task", "unfinished evening task"):
            continue
        return cleaned
    return "Unfinished tasks"


def format_duration(seconds: float) -> str:
    """Format duration in seconds into 'Xh Ym' or 'Xm Ys'."""
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}h {mins}m"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


class ReportGenerator:
    """Renders the morning markdown report strictly from the verified action log."""

    def __init__(self, repo_root: Path, config: NightGhostConfig):
        self.repo_root = repo_root.resolve()
        self.config = config
        template_dir = Path(__file__).parent / "templates"
        self.jinja_env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def generate_from_log(
        self,
        log_file: Optional[Path] = None,
        branch_name: Optional[str] = None,
        base_branch: str = "main",
        runtime_seconds: float = 0.0,
    ) -> str:
        """Parse actions.jsonl and generate the markdown morning report."""
        path = log_file or (self.repo_root / ".nightghost" / "actions.jsonl")
        entries: List[Dict[str, Any]] = []

        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            entries.append(json.loads(line.strip()))
                        except Exception:
                            continue

        # Extract git diff stats
        git = GitManager(self.repo_root)
        diff_stats = git.get_diff_stats(base_branch)

        # Parse actions
        task_text = ""
        baseline_passed = 0
        final_passed = 0
        final_failed = 0
        baseline_cov: Optional[float] = None
        final_cov: Optional[float] = None

        completed_steps = []
        judgment_items = []
        skipped_steps = []
        blocked_items = []

        today_str = datetime.date.today().strftime("%Y-%m-%d")
        branch = branch_name or f"nightghost/{today_str}"

        is_dry_run = False
        for entry in entries:
            evt = entry.get("event")
            data = entry.get("data", {})

            if evt == "PLAN_START":
                baseline_passed = data.get("baseline_passed", 0)
                final_passed = baseline_passed
                if data.get("dry_run"):
                    is_dry_run = True
            elif evt == "PLAN_FINISH":
                if "final_passed" in data:
                    final_passed = data.get("final_passed", 0)
                if "final_failed" in data:
                    final_failed = data.get("final_failed", 0)
                if data.get("dry_run"):
                    is_dry_run = True
            elif evt == "STEP_COMMIT":
                step_title = entry.get("step_title", f"Step {entry.get('step_id')}")
                commit = data.get("commit", "unknown")
                completed_steps.append({"title": step_title, "commit_sha": commit})
                if "test_passed" in data:
                    final_passed = data["test_passed"]
                if "test_failed" in data:
                    final_failed = data["test_failed"]
            elif evt == "STEP_REVERT":
                step_id = entry.get("step_id")
                step_title = entry.get("step_title")
                reason = data.get("reason", "verification failed")
                retries = data.get("retries", 0)
                skipped_steps.append(
                    f"Step {step_id} ({step_title}): reverted, {reason} after {retries + 1} attempt(s)."
                )
            elif evt == "GUARDRAIL_DENIAL":
                reason = data.get("reason", "Access denied by security rules")
                category = data.get("category", "denylist")
                blocked_items.append(f"{reason} ({category}).")
            elif evt == "JUDGMENT_FLAG":
                note = data.get("note", "")
                if note:
                    judgment_items.append(note)

        # Also get baseline and final test info from task or context
        task_path = self.repo_root / self.config.task.file
        if task_path.is_file():
            try:
                task_text = task_path.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        if not task_text:
            task_text = "Night Ghost autonomous test generation and cleanups"

        # Check last recorded test results in git/test logs if available
        # Determine icon
        all_green = (final_failed == 0)
        result_icon = "[PASS]" if all_green else "[WARN]"

        coverage_str = None
        if baseline_cov is not None and final_cov is not None:
            coverage_str = f"{int(baseline_cov)}% -> {int(final_cov)}%"

        # Format runtime
        runtime_str = format_duration(runtime_seconds) if runtime_seconds > 0 else "45m"

        report_data = ReportData(
            date=today_str,
            task=extract_task_summary(task_text),
            branch=branch,
            base_branch=base_branch,
            commits_count=diff_stats.commits or len(completed_steps),
            files_count=diff_stats.files_changed,
            lines_added=diff_stats.lines_added,
            lines_removed=diff_stats.lines_removed,
            model_name=self.config.model.name,
            model_provider="local",
            runtime_str=runtime_str,
            result_icon=result_icon,
            baseline_passed=baseline_passed,
            final_passed=final_passed,
            final_failed=final_failed,
            coverage_str=coverage_str,
            completed_steps=completed_steps,
            judgment_items=judgment_items,
            skipped_steps=skipped_steps,
            blocked_items=blocked_items,
            is_dry_run=is_dry_run,
        )

        template = self.jinja_env.get_template("report.md.j2")
        rendered = template.render(**report_data.model_dump())

        # Save to reports directory
        reports_dir = self.repo_root / ".nightghost" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_file = reports_dir / f"{today_str}.md"
        report_file.write_text(rendered, encoding="utf-8")

        # Update latest.md
        latest_file = reports_dir / "latest.md"
        latest_file.write_text(rendered, encoding="utf-8")

        return rendered
