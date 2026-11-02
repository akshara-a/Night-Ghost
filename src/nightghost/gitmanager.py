from __future__ import annotations

import datetime
import subprocess
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

from nightghost.errors import GitError


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

    # ---- branch helpers ----------------------------------------------------------------------

    def branch_exists(self, name: str) -> bool:
        proc = self._run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], check=False)
        return proc.returncode == 0

    def get_head_sha(self) -> Optional[str]:
        proc = self._run_git(["rev-parse", "HEAD"], check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def default_branch(self) -> str:
        """Best guess at the repo's mainline branch (origin/HEAD, then main, then master)."""
        proc = self._run_git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            name = proc.stdout.strip().replace("refs/remotes/origin/", "", 1)
            if self.branch_exists(name):
                return name
        for candidate in ("main", "master"):
            if self.branch_exists(candidate):
                return candidate
        current = self.get_current_branch()
        return current if current != "HEAD" else "main"

    def list_ghost_branches(self) -> List[str]:
        proc = self._run_git(
            ["for-each-ref", "--format=%(refname:short)", "refs/heads/nightghost/"], check=False
        )
        return sorted(line.strip() for line in proc.stdout.splitlines() if line.strip())

    def checkout(self, ref: str) -> None:
        proc = self._run_git(["checkout", ref], check=False)
        if proc.returncode != 0:
            raise GitError(f"Could not check out '{ref}': {proc.stderr.strip() or proc.stdout.strip()}")

    def apply_snapshot(self, sha: str) -> bool:
        """Re-apply a WIP snapshot created by create_wip_snapshot(). Returns True on success."""
        proc = self._run_git(["stash", "apply", sha], check=False)
        return proc.returncode == 0

    def diff_range(self, base: str, branch: str, stat: bool = False) -> str:
        args = ["diff"] + (["--stat"] if stat else []) + [f"{base}...{branch}"]
        proc = self._run_git(args, check=False)
        if proc.returncode != 0:
            raise GitError(proc.stderr.strip() or "git diff failed")
        return proc.stdout

    def merge_branch(self, ghost_branch: str, into: str) -> None:
        """Merge a ghost branch into `into` with a merge commit. Aborts cleanly on conflict."""
        if not self.branch_exists(ghost_branch):
            raise GitError(f"Branch '{ghost_branch}' does not exist.")
        if self.has_uncommitted_changes():
            raise GitError("Working tree has uncommitted changes; commit or stash them before merging.")
        self.checkout(into)
        proc = self._run_git(
            ["merge", "--no-ff", "-m", f"Merge {ghost_branch} (Night Ghost)", ghost_branch],
            check=False,
        )
        if proc.returncode != 0:
            self._run_git(["merge", "--abort"], check=False)
            raise GitError(
                f"Merge of '{ghost_branch}' into '{into}' failed and was aborted: "
                f"{(proc.stdout + proc.stderr).strip()}"
            )

    def delete_branch(self, name: str, force: bool = False) -> None:
        proc = self._run_git(["branch", "-D" if force else "-d", name], check=False)
        if proc.returncode != 0:
            raise GitError(proc.stderr.strip() or f"Could not delete branch '{name}'")

    def discard_branch(self, ghost_branch: str, restore_branch: Optional[str] = "main") -> None:
        """Discard the ghost branch and return cleanly to the restore branch."""
        if not self.branch_exists(ghost_branch):
            raise GitError(f"Branch '{ghost_branch}' does not exist.")
        if not restore_branch or not self.branch_exists(restore_branch):
            restore_branch = self.default_branch()
        if restore_branch == ghost_branch:
            raise GitError("Cannot restore to the branch being discarded.")

        if self.get_current_branch() == ghost_branch:
            # Throw away anything uncommitted on the ghost branch so the checkout cannot fail.
            self.revert_working_directory()
        self.checkout(restore_branch)
        self.delete_branch(ghost_branch, force=True)
