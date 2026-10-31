import json
import subprocess
from pathlib import Path
import pytest

from nightghost.config import NightGhostConfig
from nightghost.report import ReportGenerator, format_duration


@pytest.fixture
def repo_with_logs(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True)

    ng_dir = repo / ".nightghost"
    ng_dir.mkdir()
    (ng_dir / "task.md").write_text("Add tests for payment module and clean messy parts\n")

    # Create actions.jsonl with sample events
    actions = [
        {"event": "PLAN_START", "data": {"total_steps": 3}},
        {
            "event": "STEP_COMMIT",
            "step_id": 1,
            "step_title": "Added tests for charge() and refund()",
            "data": {"commit": "a1b2c3d", "test_summary": "71 passed"},
        },
        {
            "event": "STEP_COMMIT",
            "step_id": 2,
            "step_title": "Extracted duplicated currency rounding",
            "data": {"commit": "d4e5f6a", "test_summary": "71 passed"},
        },
        {
            "event": "JUDGMENT_FLAG",
            "step_id": 2,
            "data": {"note": "apply_discount returns different rounding for negative amounts."},
        },
        {
            "event": "GUARDRAIL_DENIAL",
            "step_id": 3,
            "step_title": "Update schema",
            "data": {"reason": "Access to 'migrations/0042.sql' denied by scope.deny pattern", "category": "denylist"},
        },
        {
            "event": "STEP_REVERT",
            "step_id": 3,
            "step_title": "split process_order",
            "data": {"reason": "3 tests regressed", "retries": 2},
        },
        {"event": "PLAN_FINISH", "data": {}},
    ]

    with open(ng_dir / "actions.jsonl", "w", encoding="utf-8") as f:
        for a in actions:
            f.write(json.dumps(a) + "\n")

    return repo


def test_format_duration():
    assert format_duration(45) == "45s"
    assert format_duration(150) == "2m 30s"
    assert format_duration(6120) == "1h 42m"


def test_report_generation(repo_with_logs: Path):
    config = NightGhostConfig()
    generator = ReportGenerator(repo_with_logs, config)

    report_md = generator.generate_from_log(
        branch_name="nightghost/2026-11-01",
        base_branch="main",
        runtime_seconds=6120,
    )

    # Check key sections from PLAN.md §7
    assert "# Night Ghost Report:" in report_md
    assert "Task: \"Add tests for payment module and clean messy parts\"" in report_md
    assert "Branch: nightghost/2026-11-01" in report_md
    assert "Runtime: 1h 42m" in report_md
    assert "## Result at a glance" in report_md
    assert "## What I did" in report_md
    assert "Added tests for charge() and refund(): commit a1b2c3d" in report_md
    assert "Extracted duplicated currency rounding: commit d4e5f6a" in report_md
    assert "## Needs your judgment" in report_md
    assert "apply_discount returns different rounding for negative amounts" in report_md
    assert "## Skipped / failed" in report_md
    assert "Step 3 (split process_order): reverted, 3 tests regressed after 3 attempt(s)." in report_md
    assert "## Blocked by guardrails" in report_md
    assert "migrations/0042.sql" in report_md
    assert "## How to review" in report_md
    assert "git diff main..nightghost/2026-11-01" in report_md
    assert "nightghost discard" in report_md

    # Check files created
    latest = repo_with_logs / ".nightghost" / "reports" / "latest.md"
    assert latest.is_file()
