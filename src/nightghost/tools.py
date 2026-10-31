from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from nightghost.gitmanager import GitManager
from nightghost.guardrail import Guardrail, GuardrailResult, normalize_posix
from nightghost.llm.base import ToolCall, ToolDefinition
from nightghost.testrunner import TestResult, TestRunner


class ToolExecutionResult(BaseModel):
    tool_name: str
    success: bool
    output: str
    guardrail_result: Optional[GuardrailResult] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class ToolRegistry:
    """Manages agent tools, parameter schemas, and security guardrail interceptors."""

    def __init__(
        self,
        repo_root: Path,
        guardrail: Guardrail,
        test_runner: TestRunner,
        git_manager: GitManager,
    ):
        self.repo_root = repo_root.resolve()
        self.guardrail = guardrail
        self.test_runner = test_runner
        self.git = git_manager

    def get_definitions(self) -> List[ToolDefinition]:
        """Return ToolDefinitions for LLM function calling."""
        return [
            ToolDefinition(
                name="read_file",
                description="Read the complete text content of a file within the allowed repository scope.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path of the file to read (e.g. 'src/payment.py')",
                        }
                    },
                    "required": ["path"],
                },
            ),
            ToolDefinition(
                name="write_file",
                description="Write or overwrite full text content to a file in an allowed scope. "
                            "Subject to strict guardrail verification.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative path of the target file (e.g. 'tests/test_payment.py')",
                        },
                        "content": {
                            "type": "string",
                            "description": "The exact full file content to write",
                        },
                    },
                    "required": ["path", "content"],
                },
            ),
            ToolDefinition(
                name="list_dir",
                description="List files and directories in a given repository folder.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory path relative to repository root (default '.')",
                            "default": ".",
                        }
                    },
                },
            ),
            ToolDefinition(
                name="run_tests",
                description="Execute the configured test suite or a targeted test command to verify code.",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Optional specific test command (must match allowed test runner)",
                        }
                    },
                },
            ),
            ToolDefinition(
                name="git_diff",
                description="Inspect the current working directory git diff to see pending edits.",
                parameters={
                    "type": "object",
                    "properties": {},
                },
            ),
        ]

    def execute(self, tool_call: ToolCall) -> ToolExecutionResult:
        """Execute a tool call with guardrail interception."""
        name = tool_call.name
        args = tool_call.arguments or {}

        if name == "read_file":
            return self._execute_read_file(args.get("path", ""))
        elif name == "write_file":
            return self._execute_write_file(args.get("path", ""), args.get("content", ""))
        elif name == "list_dir":
            return self._execute_list_dir(args.get("path", "."))
        elif name == "run_tests":
            return self._execute_run_tests(args.get("command"))
        elif name == "git_diff":
            return self._execute_git_diff()
        else:
            return ToolExecutionResult(
                tool_name=name,
                success=False,
                output=f"Error: Unknown tool '{name}'",
            )

    def _execute_read_file(self, raw_path: str) -> ToolExecutionResult:
        g_check = self.guardrail.validate_path(raw_path, is_write=False)
        if not g_check.allowed:
            return ToolExecutionResult(
                tool_name="read_file",
                success=False,
                output=f"Guardrail blocked read: {g_check.reason}",
                guardrail_result=g_check,
            )

        full_path = self.repo_root / raw_path
        if not full_path.is_file():
            return ToolExecutionResult(
                tool_name="read_file",
                success=False,
                output=f"File not found: {raw_path}",
            )

        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
            return ToolExecutionResult(
                tool_name="read_file",
                success=True,
                output=content,
                guardrail_result=g_check,
            )
        except Exception as e:
            return ToolExecutionResult(
                tool_name="read_file",
                success=False,
                output=f"Error reading file: {e}",
            )

    def _execute_write_file(self, raw_path: str, new_content: str) -> ToolExecutionResult:
        # 1. Path safety check
        path_check = self.guardrail.validate_path(raw_path, is_write=True)
        if not path_check.allowed:
            return ToolExecutionResult(
                tool_name="write_file",
                success=False,
                output=f"Guardrail blocked write: {path_check.reason}",
                guardrail_result=path_check,
            )

        # 2. Content secrets check
        content_check = self.guardrail.validate_content_safety(new_content, raw_path)
        if not content_check.allowed:
            return ToolExecutionResult(
                tool_name="write_file",
                success=False,
                output=f"Guardrail blocked write: {content_check.reason}",
                guardrail_result=content_check,
            )

        full_path = self.repo_root / raw_path
        old_content = ""
        if full_path.is_file():
            try:
                old_content = full_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

        # 3. Assertion modification check
        assertion_check = self.guardrail.validate_assertion_integrity(
            raw_path, old_content, new_content
        )
        if not assertion_check.allowed:
            return ToolExecutionResult(
                tool_name="write_file",
                success=False,
                output=f"Guardrail blocked write: {assertion_check.reason}",
                guardrail_result=assertion_check,
            )

        # 4. Budget check
        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()
        diff = list(difflib.ndiff(old_lines, new_lines))
        added = sum(1 for line in diff if line.startswith("+ "))
        removed = sum(1 for line in diff if line.startswith("- "))

        budget_check = self.guardrail.validate_budgets(raw_path, added, removed)
        if not budget_check.allowed:
            return ToolExecutionResult(
                tool_name="write_file",
                success=False,
                output=f"Guardrail blocked write: {budget_check.reason}",
                guardrail_result=budget_check,
            )

        # Write safely
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(new_content, encoding="utf-8")
            self.guardrail.record_change(raw_path, added, removed)
            return ToolExecutionResult(
                tool_name="write_file",
                success=True,
                output=f"Successfully wrote {len(new_content)} characters to '{raw_path}' (+{added}/-{removed} lines)",
                guardrail_result=path_check,
                details={"lines_added": added, "lines_removed": removed},
            )
        except Exception as e:
            return ToolExecutionResult(
                tool_name="write_file",
                success=False,
                output=f"Write error: {e}",
            )

    def _execute_list_dir(self, raw_path: str = ".") -> ToolExecutionResult:
        g_check = self.guardrail.validate_path(raw_path, is_write=False)
        if not g_check.allowed:
            return ToolExecutionResult(
                tool_name="list_dir",
                success=False,
                output=f"Guardrail blocked directory listing: {g_check.reason}",
                guardrail_result=g_check,
            )

        full_dir = (self.repo_root / raw_path).resolve()
        try:
            full_dir.relative_to(self.repo_root)
        except ValueError:
            return ToolExecutionResult(
                tool_name="list_dir",
                success=False,
                output=f"Cannot list directory outside repository root: {raw_path}",
            )

        if not full_dir.is_dir():
            return ToolExecutionResult(
                tool_name="list_dir",
                success=False,
                output=f"Directory not found: {raw_path}",
            )

        entries = []
        try:
            for item in sorted(full_dir.iterdir()):
                rel = normalize_posix(item.relative_to(self.repo_root))
                # Skip hidden and git
                if item.name.startswith(".") or item.name == "__pycache__":
                    continue
                kind = "dir" if item.is_dir() else "file"
                entries.append(f"{kind}: {rel}")
            return ToolExecutionResult(
                tool_name="list_dir",
                success=True,
                output="\n".join(entries) if entries else "(empty directory)",
            )
        except Exception as e:
            return ToolExecutionResult(
                tool_name="list_dir",
                success=False,
                output=f"Error listing directory: {e}",
            )

    def _execute_run_tests(self, command: Optional[str] = None) -> ToolExecutionResult:
        cmd = command if command else self.guardrail.config.commands.test
        cmd_check = self.guardrail.validate_command(cmd)
        if not cmd_check.allowed:
            return ToolExecutionResult(
                tool_name="run_tests",
                success=False,
                output=f"Guardrail blocked command: {cmd_check.reason}",
                guardrail_result=cmd_check,
            )

        res: TestResult = self.test_runner.run(cmd)
        out = f"Summary: {res.summary}\nExit code: {res.exit_code}\nOutput:\n{res.stdout}"
        if res.stderr:
            out += f"\nStderr:\n{res.stderr}"
        return ToolExecutionResult(
            tool_name="run_tests",
            success=(res.exit_code == 0),
            output=out,
            details=res.model_dump(),
        )

    def _execute_git_diff(self) -> ToolExecutionResult:
        diff_str = self.git.get_recent_diff()
        return ToolExecutionResult(
            tool_name="git_diff",
            success=True,
            output=diff_str if diff_str else "(no changes in working directory)",
        )
