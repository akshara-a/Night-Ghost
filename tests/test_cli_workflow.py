"""CLI behaviour added in the CLI overhaul: global options, exit codes, morning-after commands."""

import datetime
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nightghost.cli import app

runner = CliRunner()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def today() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


@pytest.fixture
def mock_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "README.md").write_text("# Repo\n")
    (tmp_path / ".gitignore").write_text(".nightghost/\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init")
    runner.invoke(app, ["init"])
    (tmp_path / "nightghost.yaml").write_text(
        "model:\n  provider: mock\ncommands:\n  test: 'git status'\n"
    )
    return tmp_path


def make_ghost_branch(repo: Path, filename: str = "new.txt") -> str:
    ghost = f"nightghost/{today()}"
    _git(repo, "checkout", "-b", ghost)
    (repo / filename).write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "ghost: add file")
    _git(repo, "checkout", "main")
    return ghost


def test_version_flag():
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    assert "Night Ghost v" in res.output


def test_repo_option(mock_repo: Path, tmp_path_factory, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    res = runner.invoke(app, ["-C", str(mock_repo), "status", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["branch"] == "main"


def test_init_detects_test_command_and_gitignore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text("{}")
    res = runner.invoke(app, ["init", "--yes", "--model", "qwen2.5-coder:7b"])
    assert res.exit_code == 0
    cfg = (tmp_path / "nightghost.yaml").read_text()
    assert 'test: "npm test"' in cfg
    assert "qwen2.5-coder:7b" in cfg
    assert ".nightghost/" in (tmp_path / ".gitignore").read_text()


def test_note_from_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])
    res = runner.invoke(app, ["note", "-"], input="line one\nline two\n")
    assert res.exit_code == 0
    assert "line two" in (tmp_path / ".nightghost" / "task.md").read_text()


def test_invalid_config_is_one_line_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nightghost.yaml").write_text("schedule:\n  at: 'tomorrow'\n")
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 1
    assert "Traceback" not in res.output
    assert "schedule.at" in res.output


def test_stop_then_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["stop"])
    assert (tmp_path / ".nightghost" / "STOP").exists()
    res = runner.invoke(app, ["resume"])
    assert res.exit_code == 0
    assert not (tmp_path / ".nightghost" / "STOP").exists()


def test_run_refuses_while_kill_switch_engaged(mock_repo: Path):
    runner.invoke(app, ["stop"])
    res = runner.invoke(app, ["run", "--dry-run"])
    assert res.exit_code == 1
    assert "nightghost resume" in res.output


def test_scheduled_run_requires_arming(mock_repo: Path):
    res = runner.invoke(app, ["run", "--scheduled", "--dry-run"])
    assert res.exit_code == 1
    assert "not armed" in res.output
    runner.invoke(app, ["sleep"])
    res = runner.invoke(app, ["run", "--scheduled", "--dry-run"])
    assert res.exit_code in (0, 2)


def test_run_json_and_restores_branch(mock_repo: Path):
    res = runner.invoke(app, ["run", "--json"])
    assert res.exit_code in (0, 2), res.output
    data = json.loads(res.output[res.output.index("{"):])
    assert data["branch"].startswith("nightghost/")
    # The caller's branch is restored after a live run
    assert _git(mock_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert data["branch"] in _git(mock_repo, "branch", "--list", "nightghost/*")


def test_run_restores_uncommitted_work(mock_repo: Path):
    (mock_repo / "wip.txt").write_text("half-finished\n")
    res = runner.invoke(app, ["run"])
    assert res.exit_code in (0, 2), res.output
    assert _git(mock_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (mock_repo / "wip.txt").read_text() == "half-finished\n"


def test_status_lists_ghost_branches_after_run(mock_repo: Path):
    runner.invoke(app, ["run"])
    res = runner.invoke(app, ["status", "--json"])
    assert json.loads(res.output)["ghost_branches"]


def test_report_list_and_unknown_date(mock_repo: Path):
    runner.invoke(app, ["run", "--dry-run"])
    res = runner.invoke(app, ["report", "--list"])
    assert res.exit_code == 0
    assert today() in res.output
    res = runner.invoke(app, ["report", "1999-01-01"])
    assert res.exit_code == 1
    assert "No report for 1999-01-01" in res.output


def test_diff_accept_with_delete(mock_repo: Path):
    ghost = make_ghost_branch(mock_repo)

    res = runner.invoke(app, ["diff", "--stat"])
    assert res.exit_code == 0
    assert "new.txt" in res.output

    res = runner.invoke(app, ["accept", "--yes", "--delete"])
    assert res.exit_code == 0, res.output
    assert (mock_repo / "new.txt").exists()
    assert ghost not in _git(mock_repo, "branch", "--list", "nightghost/*")


def test_discard_defaults_to_latest_branch(mock_repo: Path):
    make_ghost_branch(mock_repo, "tmp.txt")
    _git(mock_repo, "checkout", f"nightghost/{today()}")
    res = runner.invoke(app, ["discard", "--yes"])
    assert res.exit_code == 0, res.output
    assert _git(mock_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert not (mock_repo / "tmp.txt").exists()


def test_discard_with_no_branches_is_clean_error(mock_repo: Path):
    res = runner.invoke(app, ["discard", "--yes"])
    assert res.exit_code == 1
    assert "No nightghost/* branches" in res.output


def test_log_shows_actions(mock_repo: Path):
    res = runner.invoke(app, ["log"])
    assert "No action log yet" in res.output
    runner.invoke(app, ["run", "--dry-run"])
    res = runner.invoke(app, ["log", "-n", "5"])
    assert res.exit_code == 0
    assert "PLAN_" in res.output


def test_doctor_reports_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)  # not a git repo
    res = runner.invoke(app, ["doctor"])
    assert res.exit_code == 1
    assert "git repository" in res.output


def test_doctor_passes_for_mock_repo(mock_repo: Path):
    res = runner.invoke(app, ["doctor"])
    assert res.exit_code == 0, res.output
    assert "All required checks passed" in res.output


def test_schedule_all_platforms_render(mock_repo: Path):
    res = runner.invoke(app, ["schedule", "--platform", "all"])
    assert res.exit_code == 0
    out = res.output.lower()
    for needle in ("schtasks", "startcalendarinterval", "crontab", "oncalendar"):
        assert needle in out
