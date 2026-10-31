from pathlib import Path
import pytest

from nightghost.testrunner import TestResult, TestRunner


def test_parse_pytest_output():
    runner = TestRunner(Path("."))
    stdout = "=== 48 passed, 2 skipped, 1 xfailed in 1.42s ==="
    res = runner.parse_output(stdout=stdout, stderr="", exit_code=0, duration=1.42)

    assert res.passed == 48
    assert res.skipped == 2
    assert res.xfailed == 1
    assert res.failed == 0
    assert res.errors == 0
    assert res.exit_code == 0
    assert "48 passed" in res.summary


def test_parse_pytest_failures_and_errors():
    runner = TestRunner(Path("."))
    stdout = "=== 12 passed, 3 failed, 1 error in 3.10s ==="
    res = runner.parse_output(stdout=stdout, stderr="", exit_code=1, duration=3.10)

    assert res.passed == 12
    assert res.failed == 3
    assert res.errors == 1
    assert res.exit_code == 1


def test_parse_coverage():
    runner = TestRunner(Path("."))
    stdout = (
        "Name                 Stmts   Miss  Cover\n"
        "----------------------------------------\n"
        "src/payment.py          50     10    80%\n"
        "TOTAL                  100     17    83%\n"
        "=== 20 passed in 0.5s ==="
    )
    res = runner.parse_output(stdout=stdout, stderr="", exit_code=0, duration=0.5)
    assert res.coverage_pct == 83.0


def test_parse_vitest_output():
    runner = TestRunner(Path("."))
    stdout = "Tests: 15 passed, 2 failed, 17 total"
    res = runner.parse_output(stdout=stdout, stderr="", exit_code=1, duration=0.8)
    assert res.passed == 15
    assert res.failed == 2


def test_check_regression():
    baseline = TestResult(passed=10, failed=0, errors=0, exit_code=0, summary="10 passed")

    # Improvements: more tests pass, zero fail
    current_good = TestResult(passed=15, failed=0, errors=0, exit_code=0, summary="15 passed")
    regressed, reason = TestRunner.check_regression(baseline, current_good)
    assert not regressed

    # Regression: new failure
    current_fail = TestResult(passed=10, failed=1, errors=0, exit_code=1, summary="10 passed, 1 failed")
    regressed, reason = TestRunner.check_regression(baseline, current_fail)
    assert regressed
    assert "Tests regressed" in reason

    # Regression: error
    current_err = TestResult(passed=10, failed=0, errors=1, exit_code=1, summary="1 error")
    regressed, reason = TestRunner.check_regression(baseline, current_err)
    assert regressed
    assert "Errors regressed" in reason

    # Regression: passed count dropped
    current_drop = TestResult(passed=8, failed=1, errors=0, exit_code=1, summary="8 passed, 1 failed")
    regressed, reason = TestRunner.check_regression(baseline, current_drop)
    assert regressed


def test_require_green_baseline():
    unclean_baseline = TestResult(passed=5, failed=2, errors=0, exit_code=1, summary="5 passed, 2 failed")
    current = TestResult(passed=5, failed=2, errors=0, exit_code=1, summary="5 passed, 2 failed")

    # Default require_green_baseline=False -> no new regression
    reg, _ = TestRunner.check_regression(unclean_baseline, current, require_green_baseline=False)
    assert not reg

    # require_green_baseline=True -> flagged because baseline wasn't green
    reg_strict, reason = TestRunner.check_regression(unclean_baseline, current, require_green_baseline=True)
    assert reg_strict
    assert "Baseline test suite is not green" in reason
