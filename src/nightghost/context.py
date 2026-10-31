from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from nightghost.config import NightGhostConfig
from nightghost.gitmanager import GitManager
from nightghost.guardrail import normalize_posix, matches_pattern


class CodeSymbol(BaseModel):
    name: str
    kind: str  # function, class, method
    line_number: int
    docstring: Optional[str] = None


class FileSummary(BaseModel):
    path: str
    size_bytes: int
    symbols: List[CodeSymbol] = Field(default_factory=list)
    todo_markers: List[str] = Field(default_factory=list)


class ProjectContext(BaseModel):
    task_note: str = ""
    repo_map: Dict[str, FileSummary] = Field(default_factory=dict)
    recent_commits: str = ""
    recent_diff: str = ""
    todos: List[str] = Field(default_factory=list)
    test_framework: str = "pytest"
    test_files: List[str] = Field(default_factory=list)
    source_files: List[str] = Field(default_factory=list)


TODO_REGEX = re.compile(r"#\s*(TODO|FIXME|XXX|HACK)\b:?\s*(.*)", re.IGNORECASE)


class ContextBuilder:
    """Builds comprehensive codebase context: task notes, AST symbols, TODOs, and git diff."""

    def __init__(self, repo_root: Path, config: NightGhostConfig):
        self.repo_root = repo_root.resolve()
        self.config = config
        self.git = GitManager(self.repo_root)

    def load_task_note(self) -> str:
        """Read the evening task note from configured file or fallback locations."""
        candidates = [
            self.repo_root / self.config.task.file,
            self.repo_root / "NIGHTGHOST.md",
            self.repo_root / ".nightghost" / "task.md",
        ]
        for candidate in candidates:
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8").strip()
                except Exception:
                    pass
        return ""

    def _is_path_allowed(self, rel_posix: str) -> bool:
        """Check if path is in scope and not in denylist."""
        for deny_pat in self.config.scope.deny:
            if matches_pattern(rel_posix, deny_pat):
                return False
        for allow_pat in self.config.scope.allow:
            if matches_pattern(rel_posix, allow_pat):
                return True
        return False

    def _extract_python_symbols(self, code: str) -> List[CodeSymbol]:
        """Extract functions, classes, and methods using Python AST."""
        symbols: List[CodeSymbol] = []
        try:
            tree = ast.parse(code)
            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                    doc = ast.get_docstring(node)
                    symbols.append(CodeSymbol(
                        name=node.name,
                        kind="function",
                        line_number=node.lineno,
                        docstring=doc.strip().split("\n")[0] if doc else None,
                    ))
                elif isinstance(node, ast.ClassDef):
                    doc = ast.get_docstring(node)
                    symbols.append(CodeSymbol(
                        name=node.name,
                        kind="class",
                        line_number=node.lineno,
                        docstring=doc.strip().split("\n")[0] if doc else None,
                    ))
                    # Nested methods
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) or isinstance(item, ast.AsyncFunctionDef):
                            item_doc = ast.get_docstring(item)
                            symbols.append(CodeSymbol(
                                name=f"{node.name}.{item.name}",
                                kind="method",
                                line_number=item.lineno,
                                docstring=item_doc.strip().split("\n")[0] if item_doc else None,
                            ))
        except Exception:
            pass
        return symbols

    def _find_todos(self, lines: List[str], file_path: str) -> List[str]:
        """Find TODO/FIXME markers in source lines."""
        markers = []
        for i, line in enumerate(lines, start=1):
            match = TODO_REGEX.search(line)
            if match:
                marker_type = match.group(1).upper()
                msg = match.group(2).strip()
                markers.append(f"{file_path}:{i} [{marker_type}] {msg}")
        return markers

    def build_context(self) -> ProjectContext:
        """Gather full repository and task context."""
        task_note = self.load_task_note()
        recent_commits = self.git.get_recent_log(5)
        recent_diff = self.git.get_recent_diff()

        repo_map: Dict[str, FileSummary] = {}
        all_todos: List[str] = []
        test_files: List[str] = []
        source_files: List[str] = []

        # Walk repository and inspect allowed files
        for root, dirs, files in os.walk(self.repo_root):
            # Prune .git and hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]

            for file in files:
                file_full = Path(root) / file
                try:
                    rel_path = file_full.relative_to(self.repo_root)
                    rel_posix = normalize_posix(rel_path)
                except ValueError:
                    continue

                if not self._is_path_allowed(rel_posix):
                    continue

                try:
                    size = file_full.stat().st_size
                    # Limit file inspection to text files under 200KB
                    if size > 200_000:
                        continue

                    content = file_full.read_text(encoding="utf-8", errors="replace")
                    lines = content.splitlines()

                    symbols: List[CodeSymbol] = []
                    if rel_posix.endswith(".py"):
                        symbols = self._extract_python_symbols(content)

                    todos = self._find_todos(lines, rel_posix)
                    all_todos.extend(todos)

                    repo_map[rel_posix] = FileSummary(
                        path=rel_posix,
                        size_bytes=size,
                        symbols=symbols,
                        todo_markers=todos,
                    )

                    if "test" in rel_posix:
                        test_files.append(rel_posix)
                    else:
                        source_files.append(rel_posix)

                except Exception:
                    continue

        return ProjectContext(
            task_note=task_note,
            repo_map=repo_map,
            recent_commits=recent_commits,
            recent_diff=recent_diff,
            todos=all_todos,
            test_framework=self.config.commands.test,
            test_files=test_files,
            source_files=source_files,
        )
