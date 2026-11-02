from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Dict, Set
from pydantic import BaseModel, Field

from nightghost.config import NightGhostConfig


class GuardrailResult(BaseModel):
    allowed: bool
    reason: str
    category: str = "OK"
    details: Dict[str, Any] = Field(default_factory=dict)


COMMON_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.IGNORECASE), "Private Key Header"),
    (re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE), "AWS Access Key ID"),
    (re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}", re.IGNORECASE), "GitHub Token"),
    (re.compile(r"ey[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*", re.IGNORECASE), "Possible JWT / Bearer Token"),
]

ASSERTION_REGEX = re.compile(r"^\s*(assert\b|self\.assert[A-Za-z0-9_]*\(|expect\(|expect\.)")


def normalize_posix(path: Path | str) -> str:
    """Normalize path to posix-style relative string without leading slashes."""
    p_str = str(path).replace("\\", "/").strip()
    while p_str.startswith("./"):
        p_str = p_str[2:]
    return p_str.lstrip("/")


def matches_pattern(path_posix: str, pattern: str) -> bool:
    """Match a normalized posix relative path against a glob pattern."""
    pat = pattern.replace("\\", "/")
    
    # Direct match or exact file match
    if fnmatch.fnmatch(path_posix, pat):
        return True
    
    # Handle basename wildcard (e.g. *.lock matching sub/dir/poetry.lock)
    if "/" not in pat and fnmatch.fnmatch(os.path.basename(path_posix), pat):
        return True
        
    # Handle ** matching directory prefixes
    if pat.endswith("/**"):
        prefix = pat[:-3].rstrip("/")
        if path_posix == prefix or path_posix.startswith(prefix + "/"):
            return True

    if pat.startswith("**/"):
        suffix = pat[3:]
        if fnmatch.fnmatch(os.path.basename(path_posix), suffix) or fnmatch.fnmatch(path_posix, suffix):
            return True
        # Match inside any directory level
        parts = path_posix.split("/")
        for i in range(len(parts)):
            subpath = "/".join(parts[i:])
            if fnmatch.fnmatch(subpath, suffix):
                return True

    return False


class Guardrail:
    """Enforces strict path, budget, command, assertion, and secret safety constraints."""

    def __init__(self, repo_root: Path, config: NightGhostConfig):
        self.repo_root = repo_root.resolve()
        self.config = config
        self.session_files_changed: Set[str] = set()
        self.session_lines_added: int = 0
        self.session_lines_removed: int = 0

    def check_kill_switch(self) -> GuardrailResult:
        """Check if .nightghost/STOP file exists."""
        stop_file = self.repo_root / ".nightghost" / "STOP"
        if stop_file.exists():
            return GuardrailResult(
                allowed=False,
                reason="Kill switch active: .nightghost/STOP detected",
                category="KILL_SWITCH_ACTIVE",
            )
        return GuardrailResult(allowed=True, reason="Kill switch not engaged")

    def validate_path(self, target_path: str | Path, is_write: bool = True) -> GuardrailResult:
        """Validate path against traversal, symlink escapes, denylist, secrets, and allowlist."""
        # 1. Path traversal / escape check
        try:
            raw_target = Path(target_path)
            if raw_target.is_absolute():
                resolved_target = raw_target.resolve()
            else:
                resolved_target = (self.repo_root / raw_target).resolve()
        except Exception as e:
            return GuardrailResult(
                allowed=False,
                reason=f"Invalid path representation: {e}",
                category="PATH_TRAVERSAL",
                details={"target_path": str(target_path)},
            )

        try:
            rel_path = resolved_target.relative_to(self.repo_root)
        except ValueError:
            return GuardrailResult(
                allowed=False,
                reason=f"Path '{target_path}' resolves outside repository root '{self.repo_root}'",
                category="PATH_TRAVERSAL",
                details={"target_path": str(target_path), "resolved": str(resolved_target)},
            )

        rel_posix = normalize_posix(rel_path)

        # 2. Denylist check (takes precedence over allowlist)
        for deny_pat in self.config.scope.deny:
            if matches_pattern(rel_posix, deny_pat):
                return GuardrailResult(
                    allowed=False,
                    reason=f"Access to '{rel_posix}' denied by scope.deny pattern: '{deny_pat}'",
                    category="DENIED_BY_DENYLIST",
                    details={"path": rel_posix, "pattern": deny_pat},
                )

        # 3. Allowlist check for writes
        if is_write:
            matched_allow = False
            for allow_pat in self.config.scope.allow:
                if matches_pattern(rel_posix, allow_pat):
                    matched_allow = True
                    break

            if not matched_allow:
                return GuardrailResult(
                    allowed=False,
                    reason=f"Write to '{rel_posix}' denied: not in scope.allow patterns {self.config.scope.allow}",
                    category="DENIED_NOT_IN_ALLOWLIST",
                    details={"path": rel_posix, "allow_patterns": self.config.scope.allow},
                )

        return GuardrailResult(
            allowed=True,
            reason="Path authorized by guardrail",
            category="OK",
            details={"path": rel_posix, "resolved": str(resolved_target)},
        )

    def validate_content_safety(self, content: str, file_path: str = "") -> GuardrailResult:
        """Inspect proposed content for exposed secrets/credentials."""
        for pattern, desc in COMMON_SECRET_PATTERNS:
            if pattern.search(content):
                return GuardrailResult(
                    allowed=False,
                    reason=f"Writing to '{file_path}' blocked: potential secret pattern detected ({desc})",
                    category="SECRET_PATTERN_DETECTED",
                    details={"pattern": desc, "file": file_path},
                )
        return GuardrailResult(allowed=True, reason="Content safety passed")

    def validate_assertion_integrity(
        self,
        file_path: str,
        old_content: str,
        new_content: str,
    ) -> GuardrailResult:
        """Enforce the non-negotiable rule: never modify existing tests' assertions to make them pass."""
        if not self.config.safety.never_edit_existing_assertions:
            return GuardrailResult(allowed=True, reason="Assertion integrity check disabled")

        norm_path = normalize_posix(file_path)
        is_test_file = (
            "test" in norm_path
            or norm_path.startswith("tests/")
            or norm_path.endswith("_test.py")
            or "test_" in Path(norm_path).name
        )

        if not is_test_file or not old_content.strip():
            return GuardrailResult(allowed=True, reason="Not an existing test file")

        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()

        # Extract normalized assertion statements from old content
        old_assertions = [
            line.strip()
            for line in old_lines
            if ASSERTION_REGEX.match(line) and not line.strip().startswith("#")
        ]

        if not old_assertions:
            return GuardrailResult(allowed=True, reason="No existing assertions found in file")

        # Compare old assertions with new content
        # An existing assertion must still be present in the new content
        new_assertion_set = set(
            line.strip()
            for line in new_lines
            if ASSERTION_REGEX.match(line) and not line.strip().startswith("#")
        )

        missing_assertions = []
        for old_ast in old_assertions:
            if old_ast not in new_assertion_set:
                missing_assertions.append(old_ast)

        if missing_assertions:
            return GuardrailResult(
                allowed=False,
                reason=(
                    f"Modifying existing test assertions in '{file_path}' is prohibited. "
                    f"Removed/altered assertion: '{missing_assertions[0]}'"
                ),
                category="EXISTING_ASSERTION_MODIFIED",
                details={
                    "file": file_path,
                    "missing_assertions": missing_assertions[:5],
                },
            )

        return GuardrailResult(allowed=True, reason="Assertion integrity preserved")

    def validate_budgets(
        self,
        file_path: str,
        lines_added: int,
        lines_removed: int,
    ) -> GuardrailResult:
        """Ensure file count and line change budgets are strictly enforced."""
        norm_path = normalize_posix(file_path)
        will_be_new_file = norm_path not in self.session_files_changed
        new_total_files = len(self.session_files_changed) + (1 if will_be_new_file else 0)

        if new_total_files > self.config.limits.max_files_changed:
            return GuardrailResult(
                allowed=False,
                reason=(
                    f"Exceeded max_files_changed budget: {new_total_files} > "
                    f"{self.config.limits.max_files_changed}"
                ),
                category="FILE_BUDGET_EXCEEDED",
                details={
                    "current": len(self.session_files_changed),
                    "limit": self.config.limits.max_files_changed,
                },
            )

        net_lines = (self.session_lines_added + lines_added) + (self.session_lines_removed + lines_removed)
        if net_lines > self.config.limits.max_lines_changed:
            return GuardrailResult(
                allowed=False,
                reason=(
                    f"Exceeded max_lines_changed budget: {net_lines} > "
                    f"{self.config.limits.max_lines_changed}"
                ),
                category="LINE_BUDGET_EXCEEDED",
                details={
                    "current": self.session_lines_added + self.session_lines_removed,
                    "proposed_additional": lines_added + lines_removed,
                    "limit": self.config.limits.max_lines_changed,
                },
            )

        return GuardrailResult(allowed=True, reason="Budget limits within bounds")

    def record_change(self, file_path: str, lines_added: int, lines_removed: int) -> None:
        """Record accepted file and line changes towards session budgets."""
        norm = normalize_posix(file_path)
        self.session_files_changed.add(norm)
        self.session_lines_added += lines_added
        self.session_lines_removed += lines_removed

    def validate_command(self, cmd: str) -> GuardrailResult:
        """Ensure command is strictly in the configured command allowlist."""
        allowed_cmds = [
            self.config.commands.test,
            self.config.commands.lint,
            self.config.commands.format,
        ]
        
        normalized_cmd = cmd.strip()
        # Allow running exact command or command with extra test selector/filter args
        is_allowed = False
        for allowed in allowed_cmds:
            if not allowed:
                continue
            if normalized_cmd == allowed.strip() or normalized_cmd.startswith(allowed.strip()):
                is_allowed = True
                break

        # Block any dangerous chained commands or operators
        dangerous_operators = [";", "&&", "||", "|", ">", ">>", "<", "`", "$(", "\n", "\r", "&"]
        for op in dangerous_operators:
            if op in normalized_cmd:
                return GuardrailResult(
                    allowed=False,
                    reason=f"Command contains forbidden shell operator '{op}': '{cmd}'",
                    category="COMMAND_NOT_ALLOWED",
                )

        if not is_allowed:
            return GuardrailResult(
                allowed=False,
                reason=f"Command '{cmd}' is not in configured allowed commands: {allowed_cmds}",
                category="COMMAND_NOT_ALLOWED",
                details={"cmd": cmd, "allowed": allowed_cmds},
            )

        return GuardrailResult(allowed=True, reason="Command authorized")
