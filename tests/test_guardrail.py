import os
import tempfile
from pathlib import Path
import pytest

from nightghost.config import NightGhostConfig
from nightghost.guardrail import Guardrail, normalize_posix, matches_pattern


@pytest.fixture
def temp_repo(tmp_path: Path):
    """Fixture providing an initialized temporary repository directory."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "migrations").mkdir()
    (tmp_path / ".git").mkdir()
    
    (tmp_path / "src" / "app.py").write_text("def hello(): return 'world'\n", encoding="utf-8")
    (tmp_path / "tests" / "test_app.py").write_text(
        "from src.app import hello\n\ndef test_hello():\n    assert hello() == 'world'\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("SECRET_KEY=12345\n", encoding="utf-8")
    (tmp_path / "migrations" / "0001_initial.sql").write_text("CREATE TABLE users;\n", encoding="utf-8")
    (tmp_path / "cert.pem").write_text("dummy cert\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def guardrail(temp_repo: Path):
    config = NightGhostConfig()
    return Guardrail(temp_repo, config)


def test_path_normalization():
    assert normalize_posix("src\\app.py") == "src/app.py"
    assert normalize_posix("./src/app.py") == "src/app.py"
    assert normalize_posix("/src/app.py") == "src/app.py"


def test_matches_pattern():
    assert matches_pattern("src/app.py", "src/**")
    assert matches_pattern("src/deep/nested/app.py", "src/**")
    assert matches_pattern(".env", ".env*")
    assert matches_pattern(".env.production", ".env*")
    assert matches_pattern("migrations/0001.sql", "migrations/**")
    assert matches_pattern("cert.pem", "**/*.pem")
    assert matches_pattern("sub/cert.pem", "**/*.pem")
    assert matches_pattern("poetry.lock", "*.lock")


def test_guardrail_allows_src_and_tests(guardrail: Guardrail):
    res_src = guardrail.validate_path("src/app.py", is_write=True)
    assert res_src.allowed
    assert res_src.category == "OK"

    res_tests = guardrail.validate_path("tests/test_new.py", is_write=True)
    assert res_tests.allowed
    assert res_tests.category == "OK"


def test_guardrail_denies_path_traversal(guardrail: Guardrail):
    # Traversal using ..
    res = guardrail.validate_path("../outside.py", is_write=True)
    assert not res.allowed
    assert res.category == "PATH_TRAVERSAL"
    assert "resolves outside repository root" in res.reason


def test_guardrail_denies_denylist_files(guardrail: Guardrail):
    # .env
    res_env = guardrail.validate_path(".env", is_write=True)
    assert not res_env.allowed
    assert res_env.category == "DENIED_BY_DENYLIST"

    # migrations
    res_mig = guardrail.validate_path("migrations/0002_add_cols.sql", is_write=True)
    assert not res_mig.allowed
    assert res_mig.category == "DENIED_BY_DENYLIST"

    # pem key
    res_pem = guardrail.validate_path("secret.pem", is_write=True)
    assert not res_pem.allowed
    assert res_pem.category == "DENIED_BY_DENYLIST"

    # lockfile
    res_lock = guardrail.validate_path("poetry.lock", is_write=True)
    assert not res_lock.allowed
    assert res_lock.category == "DENIED_BY_DENYLIST"


def test_guardrail_denies_unauthorized_scope_paths(guardrail: Guardrail):
    # A path not in src/** or tests/**
    res = guardrail.validate_path("scripts/deploy.sh", is_write=True)
    assert not res.allowed
    assert res.category == "DENIED_NOT_IN_ALLOWLIST"


def test_guardrail_blocks_secret_content(guardrail: Guardrail):
    raw_key = "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
    res = guardrail.validate_content_safety(raw_key, "src/crypto.py")
    assert not res.allowed
    assert res.category == "SECRET_PATTERN_DETECTED"

    aws_key = "AWS_SECRET = 'AKIA1234567890ABCDEF'"
    res_aws = guardrail.validate_content_safety(aws_key, "src/config.py")
    assert not res_aws.allowed
    assert res_aws.category == "SECRET_PATTERN_DETECTED"


def test_guardrail_protects_existing_test_assertions(guardrail: Guardrail):
    old_test = (
        "def test_calc():\n"
        "    assert add(2, 3) == 5\n"
        "    assert add(-1, 1) == 0\n"
    )
    # Tampered test modifying or removing the assertion
    tampered_test = (
        "def test_calc():\n"
        "    assert add(2, 3) == 5\n"
        "    assert True  # changed assertion to pass\n"
    )
    res = guardrail.validate_assertion_integrity("tests/test_calc.py", old_test, tampered_test)
    assert not res.allowed
    assert res.category == "EXISTING_ASSERTION_MODIFIED"
    assert "Modifying existing test assertions" in res.reason


def test_guardrail_allows_adding_new_test_assertions(guardrail: Guardrail):
    old_test = "def test_calc():\n    assert add(2, 3) == 5\n"
    extended_test = (
        "def test_calc():\n"
        "    assert add(2, 3) == 5\n"
        "    assert add(0, 0) == 0\n"
        "    assert add(-1, -1) == -2\n"
    )
    res = guardrail.validate_assertion_integrity("tests/test_calc.py", old_test, extended_test)
    assert res.allowed


def test_guardrail_budget_limits(guardrail: Guardrail):
    # Test line limit
    res_lines = guardrail.validate_budgets("src/big.py", lines_added=900, lines_removed=0)
    assert not res_lines.allowed
    assert res_lines.category == "LINE_BUDGET_EXCEEDED"

    # Test file limit
    for i in range(15):
        guardrail.record_change(f"src/file_{i}.py", 1, 0)
    
    res_files = guardrail.validate_budgets("src/file_16.py", 1, 0)
    assert not res_files.allowed
    assert res_files.category == "FILE_BUDGET_EXCEEDED"


def test_guardrail_command_allowlist(guardrail: Guardrail):
    # Allowed command
    assert guardrail.validate_command("pytest -q").allowed
    assert guardrail.validate_command("pytest -q tests/test_payment.py").allowed

    # Forbidden arbitrary command
    res_rm = guardrail.validate_command("rm -rf /")
    assert not res_rm.allowed
    assert res_rm.category == "COMMAND_NOT_ALLOWED"

    # Forbidden shell chaining operator
    res_chain = guardrail.validate_command("pytest -q; rm -rf .")
    assert not res_chain.allowed
    assert res_chain.category == "COMMAND_NOT_ALLOWED"

    # Forbidden newline injection
    res_newline = guardrail.validate_command("pytest -q\ncat /etc/passwd")
    assert not res_newline.allowed
    assert res_newline.category == "COMMAND_NOT_ALLOWED"

    # Forbidden backgrounding operator
    res_bg = guardrail.validate_command("pytest -q & evil_cmd")
    assert not res_bg.allowed
    assert res_bg.category == "COMMAND_NOT_ALLOWED"


def test_guardrail_kill_switch(temp_repo: Path, guardrail: Guardrail):
    assert guardrail.check_kill_switch().allowed
    stop_file = temp_repo / ".nightghost" / "STOP"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text("STOP\n")
    check = guardrail.check_kill_switch()
    assert not check.allowed
    assert check.category == "KILL_SWITCH_ACTIVE"
