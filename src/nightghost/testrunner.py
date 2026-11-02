from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Tuple
from pydantic import BaseModel

from nightghost import netguard


class TestResult(BaseModel):
    __test__ = False
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    xfailed: int = 0
    errors: int = 0
    exit_code: int = 0
    duration: float = 0.0
    coverage_pct: Optional[float] = None
    stdout: str = ""
    stderr: str = ""
    summary: str = ""
    parse_mode: str = "regex"  # junit | regex | vitest | exit-code | error


# Regex patterns for pytest output parsing (supports both quiet and standard outputs)
PYTEST_SUMMARY_REGEX = re.compile(
    r"(?:=+\s*)?(?:(?P<passed>\d+)\s+passed)?(?:,\s*)?"
    r"(?:(?P<failed>\d+)\s+failed)?(?:,\s*)?"
    r"(?:(?P<skipped>\d+)\s+skipped)?(?:,\s*)?"
    r"(?:(?P<xfailed>\d+)\s+xfailed)?(?:,\s*)?"
    r"(?:(?P<errors>\d+)\s+error(?:s)?)?"
    r".*?in\s+(?P<duration>[\d\.]+)s",
    re.IGNORECASE,
)

# Regex pattern for Vitest/Jest output parsing
VITEST_SUMMARY_REGEX = re.compile(
    r"Tests:\s*(?:(?P<passed>\d+)\s+passed)?(?:,\s*)?"
    r"(?:(?P<failed>\d+)\s+failed)?(?:,\s*)?"
    r"(?:(?P<skipped>\d+)\s+skipped)?",
    re.IGNORECASE,
)

# Regex for coverage (e.g. TOTAL 120 12 90%)
COVERAGE_REGEX = re.compile(r"TOTAL\s+\d+\s+\d+(?:\s+\d+)?\s+(\d+)%", re.IGNORECASE)


class TestRunner:
    """Executes test and validation commands in an isolated subprocess with timeout and network limits."""
    __test__ = False

    def __init__(self, repo_root: Path, timeout_seconds: int = 300, network: str = "off"):
        self.repo_root = repo_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.network = network

    @staticmethod
    def split_command(cmd: str) -> List[str]:
        """Split a command line into argv. Never goes through a shell, so metacharacters are inert."""
        if os.name == "nt":
            parts = shlex.split(cmd, posix=False)
            return [
                p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts
            ]
        return shlex.split(cmd)

    @staticmethod
    def _is_pytest(argv: List[str]) -> bool:
        if not argv:
            return False
        stem = Path(argv[0]).stem.lower()
        if stem in ("pytest", "py.test"):
            return True
        return stem.startswith("python") and argv[1:3] == ["-m", "pytest"]

    def run(self, cmd: str) -> TestResult:
        """Run the test command safely, capturing output and parsing results."""
        start_time = time.time()

        env = os.environ.copy()
        if self.network == "off":
            netguard.apply_to_env(env)

        try:
            argv = self.split_command(cmd)
        except ValueError as e:
            return self._error_result(start_time, f"Could not parse command: {e}")
        if not argv:
            return self._error_result(start_time, "Empty test command")

        exe = shutil.which(argv[0], path=env.get("PATH"))
        if exe is None:
            return self._error_result(start_time, f"Command not found: {argv[0]}")
        argv[0] = exe

        junit_path: Optional[Path] = None
        if self._is_pytest(argv) and not any(a.startswith("--junitxml") for a in argv):
            fd, tmp = tempfile.mkstemp(prefix="nightghost-junit-", suffix=".xml")
            os.close(fd)
            junit_path = Path(tmp)
            argv.append(f"--junitxml={junit_path}")

        try:
            try:
                proc = subprocess.run(
                    argv,
                    cwd=str(self.repo_root),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    env=env,
                    encoding="utf-8",
                    errors="replace",
                )
                elapsed = time.time() - start_time
                result = self.parse_output(
                    proc.stdout or "", proc.stderr or "", proc.returncode, elapsed
                )
                if junit_path is not None:
                    self._apply_junit(result, junit_path)
                return result
            except subprocess.TimeoutExpired as e:
                elapsed = time.time() - start_time
                return TestResult(
                    exit_code=-1,
                    duration=elapsed,
                    stdout=_as_text(e.stdout),
                    stderr=_as_text(e.stderr),
                    summary=f"Test timed out after {self.timeout_seconds}s",
                    failed=1,
                    parse_mode="error",
                )
            except Exception as e:  # noqa: BLE001 - surfaced as a failed run, never crashes the pipeline
                return self._error_result(start_time, f"Execution error: {e}")
        finally:
            if junit_path is not None:
                junit_path.unlink(missing_ok=True)

    @staticmethod
    def _error_result(start_time: float, message: str) -> TestResult:
        return TestResult(
            exit_code=-2,
            duration=time.time() - start_time,
            stderr=message,
            summary=message,
            failed=1,
            parse_mode="error",
        )

    @staticmethod
    def _apply_junit(result: TestResult, junit_path: Path) -> None:
        """Override regex-scraped counts with pytest's structured JUnit XML when available."""
        try:
            root = ET.parse(junit_path).getroot()
        except (ET.ParseError, OSError):
            return
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        if not suites:
            return
        total = failures = errors = skipped = 0
        for suite in suites:
            total += int(suite.get("tests", 0))
            failures += int(suite.get("failures", 0))
            errors += int(suite.get("errors", 0))
            skipped += int(suite.get("skipped", 0))
        result.failed = failures
        result.errors = errors
        result.skipped = max(skipped - result.xfailed, 0)
        result.passed = max(total - failures - errors - skipped, 0)
        result.summary = TestRunner._summarize(result)
        result.parse_mode = "junit"

    @staticmethod
    def _summarize(r: TestResult) -> str:
        parts = []
        for count, label in (
            (r.passed, "passed"),
            (r.failed, "failed"),
            (r.skipped, "skipped"),
            (r.xfailed, "xfailed"),
            (r.errors, "errors"),
        ):
            if count:
                parts.append(f"{count} {label}")
        return ", ".join(parts) if parts else ("Passed" if r.exit_code == 0 else "Failed")

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        duration: float,
    ) -> TestResult:
        """Parse stdout/stderr to extract pass/fail/skip/coverage counts."""
        combined = stdout + "\n" + stderr

        passed = 0
        failed = 0
        skipped = 0
        xfailed = 0
        errors = 0
        mode = "regex"

        # Try pytest parsing
        match = PYTEST_SUMMARY_REGEX.search(combined)
        if match:
            if match.group("passed"):
                passed = int(match.group("passed"))
            if match.group("failed"):
                failed = int(match.group("failed"))
            if match.group("skipped"):
                skipped = int(match.group("skipped"))
            if match.group("xfailed"):
                xfailed = int(match.group("xfailed"))
            if match.group("errors"):
                errors = int(match.group("errors"))
        else:
            # Try Vitest/Jest parsing
            vitest_match = VITEST_SUMMARY_REGEX.search(combined)
            if vitest_match:
                mode = "vitest"
                if vitest_match.group("passed"):
                    passed = int(vitest_match.group("passed"))
                if vitest_match.group("failed"):
                    failed = int(vitest_match.group("failed"))
                if vitest_match.group("skipped"):
                    skipped = int(vitest_match.group("skipped"))
            else:
                # Fallback: no recognisable summary; trust only the exit code
                mode = "exit-code"
                if exit_code == 0:
                    passed = 1
                else:
                    failed = 1

        # Check coverage
        coverage_pct: Optional[float] = None
        cov_match = COVERAGE_REGEX.search(combined)
        if cov_match:
            try:
                coverage_pct = float(cov_match.group(1))
            except (ValueError, IndexError):
                pass

        summary_parts = []
        if passed:
            summary_parts.append(f"{passed} passed")
        if failed:
            summary_parts.append(f"{failed} failed")
        if skipped:
            summary_parts.append(f"{skipped} skipped")
        if xfailed:
            summary_parts.append(f"{xfailed} xfailed")
        if errors:
            summary_parts.append(f"{errors} errors")

        summary_str = ", ".join(summary_parts) if summary_parts else ("Passed" if exit_code == 0 else "Failed")

        return TestResult(
            passed=passed,
            failed=failed,
            skipped=skipped,
            xfailed=xfailed,
            errors=errors,
            exit_code=exit_code,
            duration=round(duration, 2),
            coverage_pct=coverage_pct,
            stdout=stdout,
            stderr=stderr,
            summary=summary_str,
            parse_mode=mode,
        )

    @staticmethod
    def check_regression(
        baseline: TestResult,
        current: TestResult,
        require_green_baseline: bool = False,
    ) -> Tuple[bool, str]:
        """Determine if current test run represents a regression from baseline."""
        if require_green_baseline and (baseline.failed > 0 or baseline.errors > 0):
            return True, f"Baseline test suite is not green ({baseline.summary})"

        # Regression if failures increased
        if current.failed > baseline.failed:
            diff = current.failed - baseline.failed
            return True, f"Tests regressed: {diff} new failure(s) introduced ({baseline.failed} -> {current.failed})"

        # Regression if errors increased (e.g. collection error, syntax error)
        if current.errors > baseline.errors:
            diff = current.errors - baseline.errors
            return True, f"Errors regressed: {diff} error(s) introduced ({baseline.errors} -> {current.errors})"

        # Regression if passed count dropped while failures/errors exist
        if current.passed < baseline.passed and (current.failed > 0 or current.errors > 0):
            diff = baseline.passed - current.passed
            return True, f"Passing tests dropped by {diff} ({baseline.passed} -> {current.passed})"

        # Regression if baseline exited cleanly but current did not
        if baseline.exit_code == 0 and current.exit_code != 0:
            return True, f"Test suite failed with exit code {current.exit_code}: {current.summary}"

        return False, "Tests verified: no regressions detected"


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
