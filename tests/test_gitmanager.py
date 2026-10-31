import subprocess
from pathlib import Path
import pytest

from nightghost.gitmanager import GitManager


@pytest.fixture
def git_repo(tmp_path: Path):
    """Fixture providing a temporary initialized git repository with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
    
    # Create initial file
    f = repo / "README.md"
    f.write_text("# Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo), check=True)
    return repo


def test_git_repo_detection(git_repo: Path, tmp_path: Path):
    git = GitManager(git_repo)
    assert git.is_git_repo()
    assert git.get_current_branch() == "main"

    non_repo = tmp_path / "not_a_repo"
    non_repo.mkdir()
    git_empty = GitManager(non_repo)
    assert not git_empty.is_git_repo()


def test_ghost_branch_creation(git_repo: Path):
    git = GitManager(git_repo)
    branch = git.create_or_checkout_ghost_branch("nightghost/test-branch")
    assert branch == "nightghost/test-branch"
    assert git.get_current_branch() == "nightghost/test-branch"


def test_step_commit_and_revert(git_repo: Path):
    git = GitManager(git_repo)
    git.create_or_checkout_ghost_branch("nightghost/test-branch")

    # Add a file
    new_file = git_repo / "test_file.py"
    new_file.write_text("print('test')\n", encoding="utf-8")

    commit_sha = git.commit_step("add test file")
    assert commit_sha
    log = git.get_recent_log(1)
    assert "ghost: add test file" in log

    # Revert commit
    git.revert_last_commit()
    assert not new_file.exists()


def test_wip_snapshot(git_repo: Path):
    git = GitManager(git_repo)
    assert not git.has_uncommitted_changes()
    assert git.create_wip_snapshot() is None

    # Dirty the tree
    dirty = git_repo / "dirty.txt"
    dirty.write_text("unfinished work\n", encoding="utf-8")
    assert git.has_uncommitted_changes()

    # Create snapshot
    stash_sha = git.create_wip_snapshot()
    assert stash_sha is not None


def test_diff_stats_calculation(git_repo: Path):
    git = GitManager(git_repo)
    git.create_or_checkout_ghost_branch("nightghost/test-branch")

    f1 = git_repo / "module.py"
    f1.write_text("line1\nline2\nline3\n", encoding="utf-8")
    git.commit_step("step 1")

    f2 = git_repo / "tests.py"
    f2.write_text("test1\ntest2\n", encoding="utf-8")
    git.commit_step("step 2")

    stats = git.get_diff_stats(base_ref="main")
    assert stats.commits == 2
    assert stats.files_changed == 2
    assert stats.lines_added >= 5
    assert len(stats.commit_messages) == 2


def test_discard_branch(git_repo: Path):
    git = GitManager(git_repo)
    ghost = git.create_or_checkout_ghost_branch("nightghost/to-discard")
    (git_repo / "discard_me.txt").write_text("temp\n")
    git.commit_step("discardable commit")

    git.discard_branch(ghost, restore_branch="main")
    assert git.get_current_branch() == "main"
    # Verify branch was deleted
    proc = subprocess.run(["git", "rev-parse", "--verify", ghost], cwd=str(git_repo), capture_output=True)
    assert proc.returncode != 0
