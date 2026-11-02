from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, Optional

STATE_DIRNAME = ".nightghost"


def state_dir(repo_root: Path) -> Path:
    return repo_root / STATE_DIRNAME


def stop_file(repo_root: Path) -> Path:
    return state_dir(repo_root) / "STOP"


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---- armed state (`nightghost sleep`) ------------------------------------------------------

def armed_file(repo_root: Path) -> Path:
    return state_dir(repo_root) / "armed.json"


def arm(repo_root: Path, at: str, model: str) -> Dict[str, Any]:
    data = {
        "armed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "at": at,
        "model": model,
    }
    _write(armed_file(repo_root), data)
    return data


def disarm(repo_root: Path) -> bool:
    path = armed_file(repo_root)
    if path.exists():
        path.unlink()
        return True
    return False


def load_armed(repo_root: Path) -> Optional[Dict[str, Any]]:
    return _read(armed_file(repo_root))


# ---- last run record -----------------------------------------------------------------------

def last_run_file(repo_root: Path) -> Path:
    return state_dir(repo_root) / "last_run.json"


def save_last_run(repo_root: Path, **fields: Any) -> None:
    _write(last_run_file(repo_root), fields)


def load_last_run(repo_root: Path) -> Optional[Dict[str, Any]]:
    return _read(last_run_file(repo_root))
