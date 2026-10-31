from __future__ import annotations

import datetime
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field


class DiffStats(BaseModel):
    commits: int = 0
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    commit_messages: List[str] = Field(default_factory=list)
    changed_files_list: List[str] = Field(default_factory=list)


class GitManager:
    """Manages isolated branches, WIP snapshots, per-step commits, rollbacks, and diff stats."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root.resolve()

    def _run_git(self, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command in the repository root directory."""
        return subprocess.run(
            ["git"] + args,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=check,
            encoding="utf-8",
            errors="replace",
        )

    def is_git_repo(self) -> bool:
        """Check if repo_root is inside a valid git repository."""
        proc = self._run_git(["rev-parse", "--is-inside-work-tree"], check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def get_current_branch(self) -> str:
        """Get the name of the currently checked out branch."""
        proc = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        if proc.returncode == 0:
            return proc.stdout.strip()
        return "HEAD"

    def has_uncommitted_changes(self) -> bool:
        """Check if working directory has modified or untracked files."""
        proc = self._run_git(["status", "--porcelain"], check=False)
        return bool(proc.stdout.strip())

    def create_wip_snapshot(self) -> Optional[str]:
        """Snapshot dirty working directory state safely via git stash create."""
        if not self.has_uncommitted_changes():
            return None
        # Stage all changes temporarily so git stash create captures tracked & untracked
        self._run_git(["add", "-A"], check=False)
        proc = self._run_git(["stash", "create", "nightghost-wip-snapshot"], check=False)
        stash_sha = proc.stdout.strip()
        # Reset index so working directory remains in its exact prior state
        self._run_git(["reset"], check=False)
        return stash_sha if stash_sha else None

    def create_or_checkout_ghost_branch(self, branch_name: Optional[str] = None) -> str:
        """Create or switch to the dedicated ghost branch (e.g. nightghost/YYYY-MM-DD)."""
        if not branch_name:
            today = datetime.date.today().strftime("%Y-%m-%d")
            branch_name = f"nightghost/{today}"

        # Check if branch exists
        proc = self._run_git(["rev-parse", "--verify", branch_name], check=False)
        if proc.returncode == 0:
            # Switch to existing branch
            self._run_git(["checkout", branch_name])
        else:
            # Create and switch to new branch
            self._run_git(["checkout", "-b", branch_name])

        return branch_name

    def stage_all_allowed(self) -> None:
        """Stage all modifications."""
        self._run_git(["add", "-A"])

    def commit_step(self, step_title: str) -> str:
        """Stage changes and commit with 'ghost: <step_title>'. Returns commit hash."""
        self.stage_all_allowed()
        message = f"ghost: {step_title}"
        self._run_git(["commit", "-m", message])
        proc = self._run_git(["rev-parse", "--short", "HEAD"])
        return proc.stdout.strip()

    def revert_working_directory(self) -> None:
        """Reset uncommitted changes and remove untracked files in working tree."""
        self._run_git(["reset", "HEAD"], check=False)
        self._run_git(["checkout", "--", "."], check=False)
        self._run_git(["clean", "-fd"], check=False)

    def revert_last_commit(self) -> None:
        """Revert the most recent step commit."""
        self._run_git(["reset", "--hard", "HEAD~1"])

    def get_diff_stats(self, base_ref: str = "main") -> DiffStats:
        """Calculate commit count, files changed, and lines added/removed vs base branch."""
        # Check if base_ref exists
        proc_check = self._run_git(["rev-parse", "--verify", base_ref], check=False)
        if proc_check.returncode != 0:
            # Fallback to master if main doesn't exist
            proc_master = self._run_git(["rev-parse", "--verify", "master"], check=False)
            if proc_master.returncode == 0:
                base_ref = "master"
            else:
                base_ref = "HEAD~1"

        stats = DiffStats()

        # Commits list and count
        proc_log = self._run_git(["log", f"{base_ref}..HEAD", "--oneline"], check=False)
        if proc_log.returncode == 0 and proc_log.stdout.strip():
            lines = [line.strip() for line in proc_log.stdout.strip().splitlines() if line.strip()]
            stats.commits = len(lines)
            stats.commit_messages = lines

        # Numstat for file counts and line diffs
        proc_num = self._run_git(["diff", "--numstat", f"{base_ref}..HEAD"], check=False)
        if proc_num.returncode == 0 and proc_num.stdout.strip():
            for line in proc_num.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    added_str, removed_str, filename = parts[0], parts[1], parts[2]
                    stats.files_changed += 1
                    stats.changed_files_list.append(filename)
                    if added_str.isdigit():
                        stats.lines_added += int(added_str)
                    if removed_str.isdigit():
                        stats.lines_removed += int(removed_str)

        return stats

    def get_recent_diff(self, base_ref: str = "HEAD~1") -> str:
        """Get recent diff from working tree or against previous commit."""
        proc = self._run_git(["diff", base_ref], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        # Fallback to uncommitted diff
        proc_uncommitted = self._run_git(["diff"], check=False)
        return proc_uncommitted.stdout.strip()

    def get_recent_log(self, n: int = 5) -> str:
        """Get recent commit history formatted for context."""
        proc = self._run_git(["log", f"-{n}", "--oneline"], check=False)
        if proc.returncode == 0:
            return proc.stdout.strip()
        return ""

    def discard_branch(self, ghost_branch: str, restore_branch: str = "main") -> None:
        """Discard the ghost branch and return cleanly to the restore branch."""
        # Ensure we checkout the restore branch
        proc_check = self._run_git(["rev-parse", "--verify", restore_branch], check=False)
        if proc_check.returncode != 0:
            restore_branch = "master"
        
        self._run_git(["checkout", restore_branch], check=False)
        # Delete the ghost branch
        self._run_git(["branch", "-D", ghost_branch], check=False)
