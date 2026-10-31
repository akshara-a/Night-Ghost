from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple
from pydantic import BaseModel, Field


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

    def run(self, cmd: str) -> TestResult:
        """Run the test command safely, capturing output and parsing results."""
        start_time = time.time()
        
        # Prepare environment blocking common proxy/egress variables if network is off
        env = os.environ.copy()
        if self.network == "off":
            env["http_proxy"] = "http://127.0.0.1:0"
            env["https_proxy"] = "http://127.0.0.1:0"
            env["HTTP_PROXY"] = "http://127.0.0.1:0"
            env["HTTPS_PROXY"] = "http://127.0.0.1:0"
            env["ALL_PROXY"] = "http://127.0.0.1:0"
            env["NO_PROXY"] = ""
            env["PIP_NO_INDEX"] = "1"
            env["npm_config_offline"] = "true"

        # On Windows, commands with spaces or complex arguments run through shell or parsed args
        use_shell = os.name == "nt"
        
        try:
            proc = subprocess.run(
                cmd if use_shell else shlex.split(cmd),
                cwd=str(self.repo_root),
                shell=use_shell,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=env,
                encoding="utf-8",
                errors="replace",
            )
            elapsed = time.time() - start_time
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode

        except subprocess.TimeoutExpired as e:
            elapsed = time.time() - start_time
            stdout = e.stdout or "" if hasattr(e, "stdout") and e.stdout else ""
            stderr = e.stderr or "" if hasattr(e, "stderr") and e.stderr else ""
            return TestResult(
                exit_code=-1,
                duration=elapsed,
                stdout=stdout,
                stderr=stderr,
                summary=f"Test timed out after {self.timeout_seconds}s",
                failed=1,
            )
        except Exception as e:
            elapsed = time.time() - start_time
            return TestResult(
                exit_code=-2,
                duration=elapsed,
                stderr=str(e),
                summary=f"Execution error: {e}",
                failed=1,
            )

        return self.parse_output(stdout, stderr, exit_code, elapsed)

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
                if vitest_match.group("passed"):
                    passed = int(vitest_match.group("passed"))
                if vitest_match.group("failed"):
                    failed = int(vitest_match.group("failed"))
                if vitest_match.group("skipped"):
                    skipped = int(vitest_match.group("skipped"))
            else:
                # Fallback: if exit code 0, assume at least 1 pass, else 1 fail
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
