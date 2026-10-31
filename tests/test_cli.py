import subprocess
from pathlib import Path
import pytest
from typer.testing import CliRunner

from nightghost.cli import app

runner = CliRunner()


def test_cli_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "Night Ghost v0.1.0" in result.output


def test_cli_init_and_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    
    # Init
    res_init = runner.invoke(app, ["init"])
    assert res_init.exit_code == 0
    assert (tmp_path / "nightghost.yaml").is_file()
    assert (tmp_path / ".nightghost" / "task.md").is_file()

    # Note read
    res_note_read = runner.invoke(app, ["note"])
    assert res_note_read.exit_code == 0
    assert "Unfinished Evening Task" in res_note_read.output

    # Note update
    res_note_write = runner.invoke(app, ["note", "Write tests for auth"])
    assert res_note_write.exit_code == 0
    assert "Write tests for auth" in (tmp_path / ".nightghost" / "task.md").read_text()


def test_cli_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["stop"])
    assert res.exit_code == 0
    assert (tmp_path / ".nightghost" / "STOP").is_file()


def test_cli_sleep_and_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init"])

    res_sleep = runner.invoke(app, ["sleep"])
    assert res_sleep.exit_code == 0
    assert "Armed For Sleep" in res_sleep.output

    res_sched = runner.invoke(app, ["schedule"])
    assert res_sched.exit_code == 0
    assert "nightghost run" in res_sched.output


def test_cli_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    # Initialize a git repository
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    (tmp_path / "README.md").write_text("# Repo\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True)

    runner.invoke(app, ["init"])

    # Configure mock provider for dry-run
    yaml_file = tmp_path / "nightghost.yaml"
    yaml_file.write_text("model:\n  provider: mock\ncommands:\n  test: 'git status'\n")

    res_run = runner.invoke(app, ["run", "--dry-run"])
    assert res_run.exit_code == 0
    assert "DRY RUN (simulation)" in res_run.output
    assert "Pipeline completed!" in res_run.output

    # Report check
    res_rep = runner.invoke(app, ["report"])
    assert res_rep.exit_code == 0
    assert "Night Ghost Report:" in res_rep.output
