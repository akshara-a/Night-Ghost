"""Environment checks used by `nightghost doctor` and `nightghost status`."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import yaml
from pydantic import ValidationError

from nightghost.config import NightGhostConfig
from nightghost.gitmanager import GitManager
from nightghost.state import stop_file
from nightghost.testrunner import TestRunner

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


def check_model(config: NightGhostConfig) -> Tuple[str, str, str]:
    """Return (server_label, model_label, model_state) for the configured provider.

    model_state is one of: ready, missing, offline, mock.
    """
    if config.model.provider == "mock":
        return "Mock Runner (Offline)", "Ready", "mock"
    from nightghost.llm.ollama import OllamaClient

    client = OllamaClient(config.model)
    try:
        online = client.is_available()
    except Exception:  # noqa: BLE001
        online = False
    if not online:
        return "Offline", "Unreachable", "offline"
    installed = client.list_models()
    base = config.model.name.split(":")[0]
    present = any(config.model.name in m or m.startswith(base) for m in installed)
    return "Online", "Ready (Installed)" if present else "Not downloaded", "ready" if present else "missing"


def collect_checks(repo_root: Path, config_file: Path) -> List[Check]:
    checks: List[Check] = []

    # git
    if shutil.which("git") is None:
        checks.append(Check("git installed", FAIL, "git not found on PATH", "Install git and retry."))
        return checks
    checks.append(Check("git installed", OK))

    git = GitManager(repo_root)
    in_repo = git.is_git_repo()
    if in_repo:
        has_commit = git.get_head_sha() is not None
        checks.append(
            Check("git repository", OK if has_commit else WARN,
                  git.get_current_branch(),
                  "" if has_commit else "Make an initial commit so branches can be created.")
        )
    else:
        checks.append(Check("git repository", FAIL, str(repo_root), "Run `git init` first."))

    # config
    config: Optional[NightGhostConfig] = None
    if not config_file.is_file():
        checks.append(Check("config file", WARN, f"{config_file.name} not found (using defaults)",
                            "Run `nightghost init`."))
        config = NightGhostConfig()
    else:
        try:
            config = NightGhostConfig.load(config_file)
            checks.append(Check("config file", OK, config_file.name))
        except (ValidationError, yaml.YAMLError) as e:
            first = str(e).strip().splitlines()[0]
            checks.append(Check("config file", FAIL, first, f"Fix {config_file.name}."))
            return checks

    # test command
    try:
        argv = TestRunner.split_command(config.commands.test)
    except ValueError:
        argv = []
    if not argv:
        checks.append(Check("test command", FAIL, repr(config.commands.test), "Set commands.test."))
    elif shutil.which(argv[0]) is None:
        checks.append(Check("test command", FAIL, f"'{argv[0]}' not found on PATH",
                            f"Install it or change commands.test ({config.commands.test!r})."))
    else:
        checks.append(Check("test command", OK, config.commands.test))

    # model
    server, model_label, model_state = check_model(config)
    if model_state in ("ready", "mock"):
        checks.append(Check("model", OK, f"{config.model.name} ({config.model.provider})"))
    elif model_state == "missing":
        checks.append(Check("model", FAIL, f"{config.model.name} not downloaded",
                            f"Run: ollama pull {config.model.name}"))
    else:
        checks.append(Check("model", FAIL, "Ollama server unreachable", "Run: ollama serve"))

    # task note
    task_file = repo_root / config.task.file
    if task_file.is_file() and task_file.read_text(encoding="utf-8").strip():
        checks.append(Check("task note", OK, config.task.file))
    else:
        checks.append(Check("task note", WARN, f"{config.task.file} missing or empty",
                            'Run: nightghost note "what to work on"'))

    if in_repo:
        # working tree
        if git.has_uncommitted_changes():
            checks.append(Check("working tree", WARN, "uncommitted changes (will be snapshotted)",
                                "Commit or stash if you want a pristine start."))
        else:
            checks.append(Check("working tree", OK, "clean"))

        # .gitignore
        gi = repo_root / ".gitignore"
        ignored = gi.is_file() and ".nightghost" in gi.read_text(encoding="utf-8", errors="replace")
        checks.append(
            Check(".gitignore", OK if ignored else WARN,
                  ".nightghost/ ignored" if ignored else ".nightghost/ not ignored",
                  "" if ignored else "Add `.nightghost/` to .gitignore so run state is not committed.")
        )

    # kill switch
    if stop_file(repo_root).exists():
        checks.append(Check("kill switch", WARN, "engaged", "Run `nightghost resume` before the next run."))
    else:
        checks.append(Check("kill switch", OK, "disengaged"))

    return checks


def has_failures(checks: List[Check]) -> bool:
    return any(c.status == FAIL for c in checks)


def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False
