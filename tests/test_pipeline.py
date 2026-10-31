import shutil
import subprocess
from pathlib import Path
import pytest

from nightghost.config import NightGhostConfig
from nightghost.llm.base import LLMResponse, ToolCall
from nightghost.llm.mock import MockLLMClient
from nightghost.pipeline import NightGhostPipeline


@pytest.fixture
def sample_project_dir(tmp_path: Path):
    """Set up an isolated project repository mimicking sample_repo."""
    repo = tmp_path / "payment_project"
    repo.mkdir()

    # Init git
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Developer"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=str(repo), check=True)

    # Copy files
    fixtures_src = Path(__file__).parent / "fixtures" / "sample_repo" / "src" / "payment.py"
    fixtures_tests = Path(__file__).parent / "fixtures" / "sample_repo" / "tests" / "test_payment.py"

    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    shutil.copy(fixtures_src, repo / "src" / "payment.py")
    shutil.copy(fixtures_tests, repo / "tests" / "test_payment.py")

    # Initial commit
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "Initial payment module commit"], cwd=str(repo), check=True)

    # Task note
    ng_dir = repo / ".nightghost"
    ng_dir.mkdir()
    (ng_dir / "task.md").write_text("Add tests for payment edge cases and clean up code.\n")

    return repo


def test_end_to_end_pipeline_success(sample_project_dir: Path):
    # Setup config
    config = NightGhostConfig()
    config.commands.test = "python -m pytest -q"
    config.model.provider = "mock"

    # Additional tests to write
    new_test_content = (
        "import pytest\n"
        "from src.payment import charge, refund, apply_discount, round_money\n\n"
        "def test_basic_charge():\n"
        "    res = charge(100.555, 'usd')\n"
        "    assert res['status'] == 'success'\n"
        "    assert res['amount'] == 100.56\n"
        "    assert res['currency'] == 'USD'\n\n"
        "def test_basic_refund():\n"
        "    res = refund(50.0, 'tx_100')\n"
        "    assert res['status'] == 'refunded'\n"
        "    assert res['amount'] == 50.0\n\n"
        "def test_charge_negative_raises():\n"
        "    with pytest.raises(ValueError):\n"
        "        charge(-10.0)\n\n"
        "def test_apply_discount():\n"
        "    assert apply_discount(100.0, 15.0) == 85.0\n"
    )

    # Mock responses for planner and executor:
    # 1. Planner response with 1 step
    planner_resp = LLMResponse(
        content='[{"id": 1, "title": "Add edge case unit tests for payment", "description": "Add tests", "target_files": ["tests/test_payment.py"], "verification_command": "python -m pytest -q", "kind": "test"}]'
    )
    # 2. Executor proposes writing the new tests
    step_resp = LLMResponse(
        content="Writing new edge case tests for payment module. [NEEDS JUDGMENT]: apply_discount negative amount handling is undefined.",
        tool_calls=[
            ToolCall(
                name="write_file",
                arguments={"path": "tests/test_payment.py", "content": new_test_content},
            )
        ],
    )
    # 3. Final completion for step
    finish_resp = LLMResponse(content="Step 1 complete.")

    mock_llm = MockLLMClient(canned_responses=[planner_resp, step_resp, finish_resp])

    pipeline = NightGhostPipeline(sample_project_dir, config=config, llm_client=mock_llm)
    result = pipeline.run(dry_run=False)

    assert result.success
    assert len(result.executed_steps) == 1
    assert result.executed_steps[0].status == "passed"
    assert result.executed_steps[0].commit_sha is not None

    # Check report was generated
    report_file = sample_project_dir / ".nightghost" / "reports" / "latest.md"
    assert report_file.is_file()
    report_text = report_file.read_text(encoding="utf-8")
    assert "Add edge case unit tests for payment: commit" in report_text
    assert "apply_discount negative amount handling is undefined" in report_text


def test_end_to_end_guardrail_blocking_in_pipeline(sample_project_dir: Path):
    config = NightGhostConfig()
    config.commands.test = "python -m pytest -q"
    config.model.provider = "mock"

    planner_resp = LLMResponse(
        content='[{"id": 1, "title": "Malicious or out of bounds edit", "description": "Edit .env", "target_files": [".env"], "verification_command": "python -m pytest -q"}]'
    )
    # Model attempts writing to forbidden .env
    forbidden_write = LLMResponse(
        content="Attempting to write to .env",
        tool_calls=[
            ToolCall(
                name="write_file",
                arguments={"path": ".env", "content": "SECRET=leak"},
            )
        ],
    )
    finish_resp = LLMResponse(content="Done")

    mock_llm = MockLLMClient(canned_responses=[planner_resp, forbidden_write, finish_resp])

    pipeline = NightGhostPipeline(sample_project_dir, config=config, llm_client=mock_llm)
    result = pipeline.run(dry_run=False)

    assert result.success
    # Verify denial logged in report
    report_file = sample_project_dir / ".nightghost" / "reports" / "latest.md"
    report_text = report_file.read_text(encoding="utf-8")
    assert "Blocked by guardrails" in report_text
    assert "denied by scope.deny pattern" in report_text
