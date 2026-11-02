"""Render and install OS schedules (cron, systemd, launchd, Windows Task Scheduler)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple
from xml.sax.saxutils import escape

from nightghost.errors import NightGhostError

PLATFORMS = ("windows", "macos", "linux")


def detect_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def parse_time(at: str) -> Tuple[int, int]:
    hour, minute = at.split(":")
    return int(hour), int(minute)


def executable() -> str:
    """Absolute path to the nightghost launcher, falling back to `python -m nightghost.cli`."""
    found = shutil.which("nightghost")
    return found if found else f'"{sys.executable}" -m nightghost.cli'


def _quote_exe(exe: str) -> str:
    return exe if exe.startswith('"') or " " not in exe else f'"{exe}"'


def task_slug(repo_root: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", repo_root.name).strip("-") or "repo"


def cron_marker(repo_root: Path) -> str:
    return f"# nightghost:{task_slug(repo_root)}"


def _run_line(repo_root: Path, exe: str) -> str:
    return f'{_quote_exe(exe)} -C "{repo_root}" run --scheduled'


# ---- renderers (pure functions; unit-tested) ---------------------------------------------------

def render_cron(repo_root: Path, at: str, exe: str) -> str:
    hour, minute = parse_time(at)
    log = repo_root / ".nightghost" / "run.log"
    return f'{minute} {hour} * * * {_run_line(repo_root, exe)} >> "{log}" 2>&1 {cron_marker(repo_root)}'


def render_systemd(repo_root: Path, at: str, exe: str) -> Tuple[str, str]:
    name = f"nightghost-{task_slug(repo_root)}"
    service = (
        f"# ~/.config/systemd/user/{name}.service\n"
        "[Unit]\n"
        f"Description=Night Ghost overnight run for {repo_root.name}\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={_run_line(repo_root, exe)}\n"
    )
    timer = (
        f"# ~/.config/systemd/user/{name}.timer\n"
        "[Unit]\n"
        f"Description=Run Night Ghost for {repo_root.name} nightly\n\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {at}:00\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def windows_task_name(repo_root: Path) -> str:
    return f"NightGhost-{task_slug(repo_root)}"


def windows_script_path(repo_root: Path) -> Path:
    return repo_root / ".nightghost" / "run_scheduled.cmd"


def render_windows_script(repo_root: Path, exe: str) -> str:
    log = repo_root / ".nightghost" / "run.log"
    return f'@echo off\r\n{_run_line(repo_root, exe)} >> "{log}" 2>&1\r\n'


def render_schtasks(repo_root: Path, at: str) -> List[str]:
    hour, minute = parse_time(at)
    return [
        "schtasks", "/Create", "/SC", "DAILY",
        "/TN", windows_task_name(repo_root),
        "/TR", f'"{windows_script_path(repo_root)}"',
        "/ST", f"{hour:02d}:{minute:02d}",
        "/F",
    ]


def launchd_label(repo_root: Path) -> str:
    return f"com.nightghost.{task_slug(repo_root)}"


def launchd_plist_path(repo_root: Path) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{launchd_label(repo_root)}.plist"


def render_launchd(repo_root: Path, at: str, exe: str) -> str:
    hour, minute = parse_time(at)
    args = exe.strip('"').split('" -m ') if exe.startswith('"') else [exe]
    if len(args) == 2:  # python -m nightghost.cli fallback
        program = [args[0], "-m", args[1]]
    else:
        program = [args[0]]
    program += ["-C", str(repo_root), "run", "--scheduled"]
    items = "\n".join(f"    <string>{escape(a)}</string>" for a in program)
    log = repo_root / ".nightghost" / "run.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{escape(launchd_label(repo_root))}</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>WorkingDirectory</key>
  <string>{escape(str(repo_root))}</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>{hour}</integer>
    <key>Minute</key>
    <integer>{minute}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{escape(str(log))}</string>
  <key>StandardErrorPath</key>
  <string>{escape(str(log))}</string>
</dict>
</plist>
"""


def render_for_platform(platform: str, repo_root: Path, at: str, exe: str) -> str:
    """Human-readable description of what `schedule --install` would set up."""
    if platform == "windows":
        script = render_windows_script(repo_root, exe).replace("\r\n", "\n")
        return (
            f"# {windows_script_path(repo_root)}\n{script}\n"
            "# Register the daily task:\n"
            + subprocess.list2cmdline(render_schtasks(repo_root, at))
            + "\n"
        )
    if platform == "macos":
        return f"# {launchd_plist_path(repo_root)}\n{render_launchd(repo_root, at, exe)}"
    service, timer = render_systemd(repo_root, at, exe)
    return (
        "# Crontab entry:\n"
        f"{render_cron(repo_root, at, exe)}\n\n"
        "# Or a systemd user timer:\n"
        f"{service}\n{timer}"
    )


# ---- installers (touch the OS; not unit-tested) -------------------------------------------------

def _run(cmd: List[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, input=input_text, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise NightGhostError(f"'{cmd[0]}' is not available on this system.") from e


def install(platform: str, repo_root: Path, at: str, exe: str) -> str:
    if platform == "windows":
        script = windows_script_path(repo_root)
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_bytes(render_windows_script(repo_root, exe).encode("utf-8"))
        proc = _run(render_schtasks(repo_root, at))
        if proc.returncode != 0:
            raise NightGhostError(f"schtasks failed: {(proc.stderr or proc.stdout).strip()}")
        return f"Scheduled task '{windows_task_name(repo_root)}' created for {at} daily."

    if platform == "macos":
        plist = launchd_plist_path(repo_root)
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(render_launchd(repo_root, at, exe), encoding="utf-8")
        _run(["launchctl", "unload", str(plist)])
        proc = _run(["launchctl", "load", str(plist)])
        if proc.returncode != 0:
            raise NightGhostError(f"launchctl failed: {(proc.stderr or proc.stdout).strip()}")
        return f"LaunchAgent {plist} loaded for {at} daily."

    existing = _run(["crontab", "-l"])
    lines = existing.stdout.splitlines() if existing.returncode == 0 else []
    marker = cron_marker(repo_root)
    lines = [ln for ln in lines if marker not in ln]
    lines.append(render_cron(repo_root, at, exe))
    proc = _run(["crontab", "-"], input_text="\n".join(lines) + "\n")
    if proc.returncode != 0:
        raise NightGhostError(f"crontab failed: {(proc.stderr or proc.stdout).strip()}")
    return f"Crontab entry installed for {at} daily."


def uninstall(platform: str, repo_root: Path) -> str:
    if platform == "windows":
        proc = _run(["schtasks", "/Delete", "/TN", windows_task_name(repo_root), "/F"])
        windows_script_path(repo_root).unlink(missing_ok=True)
        if proc.returncode != 0:
            return "No scheduled task found."
        return f"Scheduled task '{windows_task_name(repo_root)}' removed."

    if platform == "macos":
        plist = launchd_plist_path(repo_root)
        if not plist.exists():
            return "No LaunchAgent found."
        _run(["launchctl", "unload", str(plist)])
        plist.unlink()
        return f"LaunchAgent {plist} removed."

    existing = _run(["crontab", "-l"])
    if existing.returncode != 0:
        return "No crontab entry found."
    marker = cron_marker(repo_root)
    lines = existing.stdout.splitlines()
    kept = [ln for ln in lines if marker not in ln]
    if len(kept) == len(lines):
        return "No crontab entry found."
    _run(["crontab", "-"], input_text="\n".join(kept) + ("\n" if kept else ""))
    return "Crontab entry removed."
