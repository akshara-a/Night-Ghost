import sys
from pathlib import Path

import pytest

from nightghost import scheduler
from nightghost.testrunner import TestRunner


def test_network_shim_blocks_external_connections(tmp_path: Path):
    runner = TestRunner(tmp_path, timeout_seconds=30, network="off")
    cmd = (
        f'"{sys.executable}" -c '
        '"import socket; socket.create_connection((\'93.184.216.34\', 80), timeout=2)"'
    )
    res = runner.run(cmd)
    assert res.exit_code != 0
    assert "blocked by Night Ghost" in res.stderr


def test_network_shim_blocks_dns_for_hostnames(tmp_path: Path):
    runner = TestRunner(tmp_path, timeout_seconds=30, network="off")
    cmd = f'"{sys.executable}" -c "import socket; socket.getaddrinfo(\'example.com\', 80)"'
    res = runner.run(cmd)
    assert res.exit_code != 0
    assert "blocked by Night Ghost" in res.stderr


def test_network_shim_allows_loopback(tmp_path: Path):
    runner = TestRunner(tmp_path, timeout_seconds=30, network="off")
    cmd = f'"{sys.executable}" -c "import socket; socket.getaddrinfo(\'localhost\', 80); print(1)"'
    assert runner.run(cmd).exit_code == 0


def test_missing_command_is_reported_not_raised(tmp_path: Path):
    res = TestRunner(tmp_path).run("definitely-not-a-real-binary --flag")
    assert res.exit_code == -2
    assert "Command not found" in res.summary


def test_shell_metacharacters_are_inert(tmp_path: Path):
    marker = tmp_path / "pwned.txt"
    cmd = f'"{sys.executable}" -c "print(1)" ; echo hi > "{marker}"'
    TestRunner(tmp_path).run(cmd)
    assert not marker.exists()


def test_pytest_counts_come_from_junit(tmp_path: Path):
    (tmp_path / "test_sample.py").write_text(
        "def test_a():\n    assert True\n\n"
        "def test_b():\n    assert False\n\n"
        "import pytest\n@pytest.mark.skip\ndef test_c():\n    pass\n"
    )
    res = TestRunner(tmp_path, timeout_seconds=60, network="off").run(f'"{sys.executable}" -m pytest -q -p no:cacheprovider')
    assert res.parse_mode == "junit"
    assert (res.passed, res.failed, res.skipped) == (1, 1, 1)


# ---- scheduler rendering ------------------------------------------------------------------

REPO = Path("/work/my repo")


def test_cron_line_uses_time_and_scheduled_flag():
    line = scheduler.render_cron(REPO, "01:30", "/usr/bin/nightghost")
    assert line.startswith("30 1 * * * ")
    assert "run --scheduled" in line
    assert scheduler.cron_marker(REPO) in line


def test_windows_task_command():
    cmd = scheduler.render_schtasks(REPO, "02:05")
    assert cmd[:4] == ["schtasks", "/Create", "/SC", "DAILY"]
    assert "02:05" in cmd
    assert "/F" in cmd
    assert "NightGhost-my-repo" in cmd


def test_windows_script_logs_to_run_log():
    script = scheduler.render_windows_script(REPO, "C:\\Tools\\nightghost.exe")
    assert "run --scheduled" in script
    assert "run.log" in script


def test_launchd_plist_has_calendar_interval():
    plist = scheduler.render_launchd(REPO, "03:15", "/usr/local/bin/nightghost")
    assert "<integer>3</integer>" in plist
    assert "<integer>15</integer>" in plist
    assert "<string>--scheduled</string>" in plist


def test_systemd_units():
    service, timer = scheduler.render_systemd(REPO, "04:00", "/usr/bin/nightghost")
    assert "ExecStart=" in service
    assert "OnCalendar=*-*-* 04:00:00" in timer


def test_invalid_schedule_time_rejected():
    from nightghost.config import ScheduleConfig

    with pytest.raises(ValueError):
        ScheduleConfig(at="25:99")
    assert ScheduleConfig(at="1:5").at == "01:05"
