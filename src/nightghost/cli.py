from __future__ import annotations

import datetime
import functools
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import click
import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from nightghost import __version__, doctor as doctor_mod, scheduler, state
from nightghost.config import DEFAULT_YAML_TEMPLATE, NightGhostConfig
from nightghost.errors import NightGhostError
from nightghost.gitmanager import GitManager
from nightghost.pipeline import (
    STATUS_NO_STEPS,
    STATUS_PARTIAL,
    NightGhostPipeline,
)

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Process exit codes (documented in the README; stable for cron / CI use)
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2  # run finished but some steps were reverted or skipped

app = typer.Typer(
    name="nightghost",
    help="Night Ghost: An autonomous local agent that continues your unfinished coding work overnight.",
    add_completion=True,
    no_args_is_help=True,
)
console = Console()


# ---------------------------------------------------------------------------------------------
# Shared plumbing: global options, error handling, output helpers
# ---------------------------------------------------------------------------------------------

@dataclass
class AppState:
    repo: Path
    config_path: Optional[Path] = None
    verbose: bool = False

    @property
    def config_file(self) -> Path:
        return self.config_path or (self.repo / "nightghost.yaml")

    def load_config(self) -> NightGhostConfig:
        return NightGhostConfig.load(self.config_file)


def _state(ctx: typer.Context) -> AppState:
    obj = ctx.obj
    if isinstance(obj, AppState):
        return obj
    return AppState(repo=Path.cwd().resolve())


def ok(msg: str) -> None:
    console.print(f"[green][OK][/green] {msg}")


def warn(msg: str) -> None:
    console.print(f"[yellow][WARN][/yellow] {msg}")


def fail(msg: str) -> None:
    err = Console(stderr=True)
    err.print(f"[bold red][ERROR][/bold red] {msg}", soft_wrap=True)


def _version_flag(value: bool) -> None:
    if value:
        console.print(f"[bold cyan]Night Ghost[/bold cyan] v{__version__}")
        raise typer.Exit()


@app.callback()
def _global_options(
    ctx: typer.Context,
    repo: Optional[Path] = typer.Option(
        None, "--repo", "-C", help="Repository to operate on (default: current directory)",
        file_okay=False, resolve_path=True,
    ),
    config: Optional[Path] = typer.Option(
        None, "--config", help="Path to a nightghost.yaml (default: <repo>/nightghost.yaml)",
        dir_okay=False, resolve_path=True,
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed output and tracebacks"),
    version_flag: bool = typer.Option(
        False, "--version", callback=_version_flag, is_eager=True, help="Show version and exit"
    ),
):
    ctx.obj = AppState(repo=(repo or Path.cwd()).resolve(), config_path=config, verbose=verbose)


def command(name: Optional[str] = None, **kwargs: Any) -> Callable:
    """Register a command, turning expected failures into one-line errors instead of tracebacks."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kw: Any):
            try:
                return fn(*args, **kw)
            except (typer.Exit, typer.Abort, click.ClickException):
                raise
            except NightGhostError as e:
                _fail_and_exit(str(e), e)
            except ValidationError as e:
                lines = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
                _fail_and_exit("Invalid nightghost.yaml - " + "; ".join(lines), e)
            except yaml.YAMLError as e:
                _fail_and_exit(f"nightghost.yaml is not valid YAML: {e}", e)
            except subprocess.CalledProcessError as e:
                _fail_and_exit(f"git failed: {(e.stderr or '').strip() or e}", e)
            except FileNotFoundError as e:
                _fail_and_exit(f"File or program not found: {e.filename or e}", e)

        return app.command(name, **kwargs)(wrapper)

    return decorator


def _fail_and_exit(message: str, exc: BaseException) -> None:
    ctx = click.get_current_context(silent=True)
    if ctx is not None and isinstance(ctx.obj, AppState) and ctx.obj.verbose:
        raise exc
    fail(message)
    raise typer.Exit(code=EXIT_ERROR)


def _resolve_ghost_branch(git: GitManager, repo: Path, branch: Optional[str]) -> str:
    """Pick the ghost branch to act on: explicit arg, last run, today's, or the newest one."""
    if branch:
        name = branch if "/" in branch else f"nightghost/{branch}"
        if not git.branch_exists(name):
            existing = ", ".join(git.list_ghost_branches()) or "none"
            raise NightGhostError(f"Branch '{name}' does not exist (ghost branches: {existing}).")
        return name
    last = state.load_last_run(repo)
    if last and last.get("branch") and git.branch_exists(last["branch"]):
        return last["branch"]
    today = f"nightghost/{datetime.date.today():%Y-%m-%d}"
    if git.branch_exists(today):
        return today
    branches = git.list_ghost_branches()
    if branches:
        return branches[-1]
    raise NightGhostError("No nightghost/* branches found. Run `nightghost run` first.")


def _base_for(git: GitManager, repo: Path, override: Optional[str]) -> str:
    if override:
        return override
    last = state.load_last_run(repo)
    if last and last.get("base_branch") and git.branch_exists(last["base_branch"]):
        return last["base_branch"]
    return git.default_branch()


def _require_git(git: GitManager, repo: Path) -> None:
    if not git.is_git_repo():
        raise NightGhostError(f"'{repo}' is not a git repository.")


# ---------------------------------------------------------------------------------------------
# init / note
# ---------------------------------------------------------------------------------------------

def detect_test_command(repo: Path) -> str:
    """Guess the project's test command from well-known files."""
    if (repo / "package.json").is_file():
        return "npm test"
    if (repo / "go.mod").is_file():
        return "go test ./..."
    if (repo / "Cargo.toml").is_file():
        return "cargo test"
    return "pytest -q"


def _ensure_gitignore(repo: Path) -> bool:
    """Add `.nightghost/` to .gitignore. Returns True if the file changed."""
    gi = repo / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    if ".nightghost" in text:
        return False
    sep = "" if not text or text.endswith("\n") else "\n"
    gi.write_text(f"{text}{sep}.nightghost/\n", encoding="utf-8")
    return True


@command()
def version():
    """Show Night Ghost version."""
    console.print(f"[bold cyan]Night Ghost[/bold cyan] v{__version__}")


@command()
def init(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing configuration"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Ollama model to configure"),
    yes: bool = typer.Option(False, "--yes", "--non-interactive", "-y",
                             help="Accept all suggestions without prompting"),
):
    """Initialize Night Ghost configuration and workspace in the repository."""
    st = _state(ctx)
    repo_root = st.repo
    config_file = repo_root / "nightghost.yaml"
    task_dir = repo_root / ".nightghost"
    task_file = task_dir / "task.md"

    if config_file.exists() and not force:
        console.print(f"[yellow]Configuration file already exists:[/yellow] {config_file}")
        console.print("Use [bold]--force[/bold] to overwrite.")
        return

    test_cmd = detect_test_command(repo_root)
    template = DEFAULT_YAML_TEMPLATE.replace('test: "pytest -q"', f'test: "{test_cmd}"')
    if model:
        template = template.replace("name: gemma4:e2b", f"name: {model}")
    config_file.write_text(template, encoding="utf-8")
    task_dir.mkdir(parents=True, exist_ok=True)

    if not task_file.exists() or force:
        task_file.write_text(
            "# Unfinished Evening Task\n\n"
            "Add tests for edge cases and clean up any messy parts.\n",
            encoding="utf-8",
        )

    gitignore_note = ""
    gi = repo_root / ".gitignore"
    needs_ignore = ".nightghost" not in (gi.read_text(encoding="utf-8") if gi.is_file() else "")
    if needs_ignore:
        interactive = sys.stdin is not None and sys.stdin.isatty()
        if yes or (interactive and typer.confirm("Add .nightghost/ to .gitignore?", default=True)):
            _ensure_gitignore(repo_root)
            gitignore_note = "Added [cyan].nightghost/[/cyan] to .gitignore\n"
        else:
            gitignore_note = "[yellow]Tip: add `.nightghost/` to .gitignore[/yellow]\n"

    console.print(
        Panel.fit(
            f"[bold green]Night Ghost Initialized Successfully![/bold green]\n\n"
            f"Created [cyan]{config_file.name}[/cyan] (test command: [bold]{test_cmd}[/bold])\n"
            f"Created [cyan]{task_file.relative_to(repo_root)}[/cyan]\n"
            f"{gitignore_note}\n"
            f"[dim]Next: `nightghost doctor`, then `nightghost note \"...\"` and `nightghost sleep`.[/dim]",
            title="Night Ghost",
            border_style="green",
        )
    )


@command()
def note(
    ctx: typer.Context,
    task_text: Optional[str] = typer.Argument(
        None, help="The task instructions for tonight's run. Use '-' to read from stdin."
    ),
    edit: bool = typer.Option(False, "--edit", "-e", help="Open the note in $EDITOR"),
):
    """Set or display the evening task note."""
    st = _state(ctx)
    config = st.load_config()
    task_file = st.repo / config.task.file

    if task_text == "-":
        task_text = sys.stdin.read()
        if not task_text.strip():
            raise NightGhostError("Nothing received on stdin.")

    if edit:
        current = task_file.read_text(encoding="utf-8") if task_file.is_file() else ""
        edited = typer.edit(current)
        if edited is None or not edited.strip():
            console.print("[dim]No changes saved.[/dim]")
            return
        task_text = edited

    if task_text:
        task_file.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_text(task_text.strip() + "\n", encoding="utf-8")
        ok(f"Evening note saved to [cyan]{config.task.file}[/cyan]:")
        console.print(Panel(task_text.strip(), border_style="cyan"), markup=False)
    elif task_file.is_file():
        content = task_file.read_text(encoding="utf-8").strip()
        console.print(Panel(content, title=f"Task Note ({config.task.file})", border_style="cyan"))
    else:
        console.print(f"[yellow]No task note found at {config.task.file}.[/yellow]")
        console.print('Set one with: [bold]nightghost note "Your instructions here"[/bold]')


# ---------------------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------------------

_STEP_EVENTS = {
    "STEP_START", "STEP_COMMIT", "STEP_RETRY", "STEP_REVERT", "GUARDRAIL_DENIAL",
    "KILL_SWITCH_TRIGGERED", "RUNTIME_LIMIT_EXCEEDED", "JUDGMENT_FLAG",
}


def _make_listener(out: Console, verbose: bool, status_ctl: Any) -> Callable[[str, Dict[str, Any]], None]:
    live_status = out.is_terminal and not verbose

    def listener(event: str, data: Dict[str, Any]) -> None:
        sid, title = data.get("step_id"), data.get("step_title")
        if event == "PHASE":
            if live_status and status_ctl is not None:
                status_ctl.update(f"[cyan]{data['message']}[/cyan]")
            else:
                out.print(f"[dim]... {data['message']}[/dim]", markup=True)
        elif event == "PLAN_READY":
            out.print(f"[bold]Plan ready:[/bold] {data.get('total_steps', 0)} step(s)")
        elif event == "STEP_START":
            if live_status and status_ctl is not None:
                status_ctl.update(f"[cyan]Step {sid}: {title}[/cyan]")
            out.print(f"[cyan]>[/cyan] Step {sid}: {title}")
        elif event == "STEP_COMMIT":
            out.print(f"  [green][OK][/green] committed {data.get('commit')} ({data.get('test_summary')})")
        elif event == "STEP_RETRY":
            out.print(f"  [yellow][RETRY][/yellow] attempt {data.get('attempt')}: {data.get('reason')}")
        elif event == "STEP_REVERT":
            out.print(f"  [red][REVERT][/red] {data.get('reason')}")
        elif event == "GUARDRAIL_DENIAL":
            out.print(f"  [red][BLOCKED][/red] {data.get('category')}: {data.get('reason')}")
        elif event == "JUDGMENT_FLAG":
            out.print(f"  [yellow][NEEDS JUDGMENT][/yellow] {data.get('note')}")
        elif event in ("KILL_SWITCH_TRIGGERED", "RUNTIME_LIMIT_EXCEEDED"):
            out.print(f"  [red][STOPPED][/red] {data.get('reason') or event}")
        elif verbose and event not in _STEP_EVENTS:
            detail = {k: v for k, v in data.items() if v is not None and k not in ("step_id", "step_title")}
            out.print(f"[dim]{event} step={sid} {json.dumps(detail, default=str)[:200]}[/dim]", markup=True)

    return listener


@command()
def run(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Plan and simulate the pipeline without writing edits or commits"
    ),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override the LLM model name"),
    scheduled: bool = typer.Option(
        False, "--scheduled", help="Invoked by the OS scheduler: only runs if armed with `nightghost sleep`"
    ),
    json_out: bool = typer.Option(False, "--json", help="Print a machine-readable JSON summary"),
):
    """Wake and execute the overnight coding pipeline.

    Exit codes: 0 success, 1 error, 2 partial (some steps reverted or skipped).
    """
    st = _state(ctx)
    config = st.load_config()
    out = Console(quiet=json_out)

    if scheduled and not state.load_armed(st.repo):
        fail("Night Ghost is not armed for this repository. Run `nightghost sleep` first.")
        raise typer.Exit(code=EXIT_ERROR)

    out.print(
        Panel.fit(
            f"[bold cyan]Night Ghost Active[/bold cyan]\n"
            f"Model: [green]{model or config.model.name}[/green] ({config.model.provider})\n"
            f"Mode: {'[yellow]DRY RUN (simulation)[/yellow]' if dry_run else '[bold green]LIVE RUN[/bold green]'}",
            title="Overnight Run",
            border_style="cyan",
        )
    )

    status_ctl = out.status("[cyan]Starting...[/cyan]") if out.is_terminal and not st.verbose else None
    listener = _make_listener(out, st.verbose, status_ctl)
    pipeline = NightGhostPipeline(st.repo, config, on_event=listener)
    if status_ctl is not None:
        status_ctl.start()
    try:
        res = pipeline.run(dry_run=dry_run, model_override=model)
    finally:
        if status_ctl is not None:
            status_ctl.stop()

    if json_out:
        typer.echo(json.dumps({
            "status": res.status,
            "success": res.success,
            "branch": res.branch_name,
            "base_branch": res.base_branch,
            "dry_run": dry_run,
            "steps_planned": len(res.plan.steps),
            "steps_passed": res.steps_passed,
            "steps_reverted": res.steps_reverted,
            "steps_skipped": res.steps_skipped,
            "tests_before": res.baseline_tests.passed,
            "tests_after": res.final_tests.passed if res.final_tests else None,
            "elapsed_seconds": round(res.runtime_seconds, 1),
            "error": res.error_message,
        }))
    elif not res.success:
        fail(f"Pipeline halted: {res.error_message}")
    else:
        if res.status == STATUS_PARTIAL:
            out.print("\n[bold yellow][PARTIAL] Pipeline finished with reverted or skipped steps.[/bold yellow]")
        elif res.status == STATUS_NO_STEPS:
            out.print("\n[bold yellow][OK] Pipeline finished, but the planner found nothing to do.[/bold yellow]")
        else:
            out.print("\n[bold green][OK] Pipeline completed![/bold green]")
        out.print(f"Branch: [cyan]{res.branch_name}[/cyan]"
                  + (" [dim](continued existing branch)[/dim]" if res.branch_existed else ""))
        out.print(f"Steps Planned: [bold]{len(res.plan.steps)}[/bold]")
        out.print(f"Steps Executed: [bold]{len(res.executed_steps)}[/bold] "
                  f"([green]{res.steps_passed} passed[/green], [red]{res.steps_reverted} reverted[/red], "
                  f"[yellow]{res.steps_skipped} skipped[/yellow])")
        if res.final_tests:
            out.print(f"Tests: [bold]{res.baseline_tests.passed} -> {res.final_tests.passed}[/bold] passing "
                      f"({res.final_tests.failed} failing)")
        out.print(f"Elapsed: [dim]{round(res.runtime_seconds, 1)}s[/dim]")
        if res.wip_snapshot and res.wip_restored is not None:
            restored = "restored to your working tree" if res.wip_restored else \
                f"could not be re-applied; recover with: git stash apply {res.wip_snapshot}"
            out.print(f"Your uncommitted work was snapshotted and {restored}.")
        out.print("\nReview: [bold cyan]nightghost report[/bold cyan], [bold cyan]nightghost diff[/bold cyan], "
                  "then [bold cyan]nightghost accept[/bold cyan] or [bold cyan]nightghost discard[/bold cyan]")

    if not res.success:
        raise typer.Exit(code=EXIT_ERROR)
    if res.status == STATUS_PARTIAL:
        raise typer.Exit(code=EXIT_PARTIAL)


# ---------------------------------------------------------------------------------------------
# morning-after commands: report / diff / accept / discard / log
# ---------------------------------------------------------------------------------------------

def _report_dates(repo: Path) -> List[str]:
    reports_dir = state.state_dir(repo) / "reports"
    if not reports_dir.is_dir():
        return []
    return sorted(p.stem for p in reports_dir.glob("*.md") if p.stem != "latest")


@command()
def report(
    ctx: typer.Context,
    date: Optional[str] = typer.Argument(
        None, help="Date of report to display (YYYY-MM-DD), defaults to latest"
    ),
    list_dates: bool = typer.Option(False, "--list", "-l", help="List available report dates"),
):
    """View the morning report in your terminal."""
    st = _state(ctx)
    reports_dir = state.state_dir(st.repo) / "reports"
    dates = _report_dates(st.repo)

    if list_dates:
        if not dates:
            console.print("[yellow]No reports yet.[/yellow] Run Night Ghost first with: [bold]nightghost run[/bold]")
        for d in dates:
            console.print(d)
        return

    if date:
        target = reports_dir / f"{date}.md"
        if not target.is_file():
            hint = f" Available: {', '.join(dates)}" if dates else " No reports exist yet."
            raise NightGhostError(f"No report for {date}.{hint}")
    else:
        target = reports_dir / "latest.md"
        if not target.is_file() and dates:
            target = reports_dir / f"{dates[-1]}.md"
        if not target.is_file():
            console.print("[yellow]No morning report found yet.[/yellow]")
            console.print("Run Night Ghost first with: [bold]nightghost run[/bold]")
            return

    console.print(Markdown(target.read_text(encoding="utf-8")))


@command(name="diff")
def diff_cmd(
    ctx: typer.Context,
    branch: Optional[str] = typer.Argument(None, help="Ghost branch or date (default: latest run)"),
    base: Optional[str] = typer.Option(None, "--base", "-b", help="Branch to compare against"),
    stat: bool = typer.Option(False, "--stat", "-s", help="Show a diffstat instead of the full diff"),
):
    """Show what the ghost changed (diff of the ghost branch vs. its base)."""
    st = _state(ctx)
    git = GitManager(st.repo)
    _require_git(git, st.repo)
    target = _resolve_ghost_branch(git, st.repo, branch)
    base_branch = _base_for(git, st.repo, base)
    text = git.diff_range(base_branch, target, stat=stat)
    if not text.strip():
        console.print(f"[dim]No differences between {base_branch} and {target}.[/dim]")
        return
    if console.is_terminal and not stat:
        console.print(Syntax(text, "diff", word_wrap=False))
    else:
        typer.echo(text)


@command()
def accept(
    ctx: typer.Context,
    branch: Optional[str] = typer.Argument(None, help="Ghost branch or date (default: latest run)"),
    into: Optional[str] = typer.Option(None, "--into", help="Branch to merge into (default: the run's base)"),
    delete: bool = typer.Option(False, "--delete", "-d", help="Delete the ghost branch after merging"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Merge the ghost branch into your main branch (the morning 'looks good' button)."""
    st = _state(ctx)
    git = GitManager(st.repo)
    _require_git(git, st.repo)
    target = _resolve_ghost_branch(git, st.repo, branch)
    dest = _base_for(git, st.repo, into)

    stats = git.diff_range(dest, target, stat=True).strip().splitlines()
    summary = stats[-1].strip() if stats else "no changes"
    if not yes and not typer.confirm(f"Merge '{target}' into '{dest}' ({summary})?"):
        console.print("[dim]Aborted.[/dim]")
        return

    git.merge_branch(target, into=dest)
    ok(f"Merged [cyan]{target}[/cyan] into [cyan]{dest}[/cyan].")
    if delete:
        git.delete_branch(target)
        ok(f"Deleted branch {target}.")


@command()
def discard(
    ctx: typer.Context,
    branch: Optional[str] = typer.Argument(
        None, help="The ghost branch (or date) to discard (default: latest run)"
    ),
    restore: Optional[str] = typer.Option(
        None, "--restore", "-r", help="Branch to check out after discarding (default: the run's base)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Discard overnight ghost branch and cleanly reset working tree."""
    st = _state(ctx)
    git = GitManager(st.repo)
    _require_git(git, st.repo)
    target_branch = _resolve_ghost_branch(git, st.repo, branch)
    restore_to = restore or _base_for(git, st.repo, None)

    if not yes and not typer.confirm(f"Are you sure you want to discard branch '{target_branch}'?"):
        console.print("[dim]Aborted.[/dim]")
        return

    git.discard_branch(target_branch, restore_branch=restore_to)
    ok(f"Branch '{target_branch}' discarded. Restored '{git.get_current_branch()}'.")


def _format_log_line(raw: str) -> Optional[str]:
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    ts = str(entry.get("timestamp", ""))[11:19]
    step = f" #{entry['step_id']}" if entry.get("step_id") is not None else ""
    title = f" {entry['step_title']}" if entry.get("step_title") else ""
    data = entry.get("data") or {}
    detail = " ".join(
        f"{k}={v}" for k, v in data.items()
        if k in ("commit", "reason", "tool", "test_summary", "category", "total_steps", "note")
    )
    return f"{ts} {entry.get('event', '?'):<22}{step}{title} {detail}".rstrip()


@command(name="log")
def log_cmd(
    ctx: typer.Context,
    lines: int = typer.Option(30, "--lines", "-n", help="Number of recent entries to show"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep streaming new entries (Ctrl+C to stop)"),
):
    """Show (or follow) the structured action log of the ghost's work."""
    st = _state(ctx)
    log_file = state.state_dir(st.repo) / "actions.jsonl"
    if not log_file.is_file():
        if not follow:
            console.print("[yellow]No action log yet.[/yellow] Run Night Ghost first with: [bold]nightghost run[/bold]")
            return
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.touch()

    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        recent = f.read().splitlines()[-lines:]
        for raw in recent:
            line = _format_log_line(raw)
            if line:
                console.print(line, markup=False, highlight=False, soft_wrap=True)
        if not follow:
            return
        try:
            while True:
                raw = f.readline()
                if not raw:
                    time.sleep(0.5)
                    continue
                line = _format_log_line(raw)
                if line:
                    console.print(line, markup=False, highlight=False, soft_wrap=True)
        except KeyboardInterrupt:
            pass


# ---------------------------------------------------------------------------------------------
# status / doctor
# ---------------------------------------------------------------------------------------------

@command()
def status(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
):
    """Check current branch, task note, and safety status."""
    st = _state(ctx)
    config = st.load_config()
    git = GitManager(st.repo)
    _require_git(git, st.repo)

    server, model_label, model_state = doctor_mod.check_model(config)
    stop_engaged = state.stop_file(st.repo).exists()
    armed = state.load_armed(st.repo)
    last = state.load_last_run(st.repo)
    dirty = git.has_uncommitted_changes()

    data = {
        "branch": git.get_current_branch(),
        "uncommitted_changes": dirty,
        "scheduled_time": config.schedule.at,
        "armed": bool(armed),
        "model": config.model.name,
        "provider": config.model.provider,
        "test_command": config.commands.test,
        "model_server": server,
        "model_status": model_state,
        "kill_switch_engaged": stop_engaged,
        "ghost_branches": git.list_ghost_branches(),
        "last_run": last,
    }
    if json_out:
        typer.echo(json.dumps(data, indent=2, default=str))
        return

    colour = {"ready": "green", "mock": "green", "missing": "yellow", "offline": "red"}[model_state]
    model_text = model_label
    if model_state == "missing":
        model_text = f"Not downloaded (Run: ollama pull {config.model.name})"
    if model_state == "offline":
        server = "Offline (Run: ollama serve)"

    table = Table(title="Night Ghost Status", border_style="cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")
    table.add_row("Current Git Branch", data["branch"])
    table.add_row("Uncommitted Changes", "Yes (snapshot armed)" if dirty else "Clean")
    table.add_row("Armed", f"[green]Yes[/green] (since {armed['armed_at']})" if armed else "No (run `nightghost sleep`)")
    table.add_row("Scheduled Time", config.schedule.at)
    table.add_row("Configured Model", f"{config.model.name} ({config.model.provider})")
    table.add_row("Test Command", config.commands.test)
    server_colour = "red" if model_state == "offline" else "green" if model_state != "mock" else "cyan"
    table.add_row("Model Server" if model_state == "mock" else "Ollama Server", f"[{server_colour}]{server}[/{server_colour}]")
    table.add_row("Model Status", f"[{colour}]{model_text}[/{colour}]")
    table.add_row("Kill Switch (.nightghost/STOP)",
                  "[bold red]ENGAGED[/bold red] (run `nightghost resume`)" if stop_engaged else "Disengaged (Ready)")
    table.add_row("Ghost Branches", ", ".join(data["ghost_branches"]) or "none")
    if last:
        table.add_row("Last Run", f"{last.get('date')} - {last.get('status')} "
                                  f"({last.get('steps_passed')}/{last.get('steps_planned')} steps)")
    console.print(table)


@command(name="doctor")
def doctor_cmd(
    ctx: typer.Context,
    json_out: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
):
    """Check your environment (git, config, test command, model) and say how to fix problems."""
    st = _state(ctx)
    checks = doctor_mod.collect_checks(st.repo, st.config_file)
    failed = doctor_mod.has_failures(checks)

    if json_out:
        typer.echo(json.dumps([c.__dict__ for c in checks], indent=2))
    else:
        icon = {doctor_mod.OK: "[green][OK][/green]  ", doctor_mod.WARN: "[yellow][WARN][/yellow]",
                doctor_mod.FAIL: "[red][FAIL][/red]  "}
        for c in checks:
            detail = f" - {c.detail}" if c.detail else ""
            console.print(f"{icon[c.status]} {c.name}{detail}", markup=True, highlight=False)
            if c.hint and c.status != doctor_mod.OK:
                console.print(f"         [dim]{c.hint}[/dim]")
        console.print()
        if failed:
            console.print("[bold red]Some checks failed. Fix them before `nightghost run`.[/bold red]")
        else:
            console.print("[bold green]All required checks passed.[/bold green]")
    if failed:
        raise typer.Exit(code=EXIT_ERROR)


# ---------------------------------------------------------------------------------------------
# stop / resume / sleep / schedule
# ---------------------------------------------------------------------------------------------

@command()
def stop(ctx: typer.Context):
    """Engage emergency kill switch (.nightghost/STOP) to halt any running overnight execution."""
    st = _state(ctx)
    sf = state.stop_file(st.repo)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text("STOP\n", encoding="utf-8")
    console.print(
        Panel.fit(
            "[bold red]Kill switch engaged![/bold red]\n"
            f"Created [cyan]{sf}[/cyan]. Any running or next step will immediately halt.\n"
            "Clear it with [bold]nightghost resume[/bold].",
            title="Emergency Stop",
            border_style="red",
        )
    )


@command()
def resume(ctx: typer.Context):
    """Disengage the kill switch so Night Ghost can run again."""
    st = _state(ctx)
    sf = state.stop_file(st.repo)
    if sf.exists():
        sf.unlink()
        ok("Kill switch disengaged. Night Ghost can run again.")
    else:
        console.print("[dim]Kill switch was not engaged.[/dim]")


@command()
def sleep(
    ctx: typer.Context,
    install: bool = typer.Option(False, "--install", help="Also install the OS schedule (see `schedule`)"),
    cancel: bool = typer.Option(False, "--cancel", help="Disarm; scheduled runs will no longer execute"),
):
    """Arm the overnight run (scheduled runs only execute while armed)."""
    st = _state(ctx)
    config = st.load_config()

    if cancel:
        if state.disarm(st.repo):
            ok("Disarmed. Scheduled runs will be skipped until you run `nightghost sleep` again.")
        else:
            console.print("[dim]Night Ghost was not armed.[/dim]")
        return

    state.arm(st.repo, at=config.schedule.at, model=config.model.name)
    extra = ""
    if install:
        extra = "\n" + scheduler.install(scheduler.detect_platform(), st.repo, config.schedule.at,
                                         scheduler.executable())
    else:
        extra = ("\n[dim]No schedule installed yet: run `nightghost schedule --install`, "
                 "or trigger `nightghost run` yourself.[/dim]")

    console.print(
        Panel.fit(
            f"[bold cyan]Night Ghost Armed For Sleep[/bold cyan]\n\n"
            f"Scheduled at: [bold]{config.schedule.at}[/bold]\n"
            f"Model: [bold green]{config.model.name}[/bold green]\n"
            f"Task file: [bold]{config.task.file}[/bold]{extra}",
            title="Good Night",
            border_style="cyan",
        )
    )


@command()
def schedule(
    ctx: typer.Context,
    install: bool = typer.Option(False, "--install", help="Install the schedule on this machine"),
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove the installed schedule"),
    platform: Optional[str] = typer.Option(
        None, "--platform", "-p", help="windows | macos | linux | all (default: this machine)"
    ),
):
    """Show or install the nightly schedule (Task Scheduler, launchd, cron/systemd)."""
    st = _state(ctx)
    config = st.load_config()
    exe = scheduler.executable()
    target = (platform or scheduler.detect_platform()).lower()
    if target != "all" and target not in scheduler.PLATFORMS:
        raise NightGhostError(f"Unknown platform '{platform}'. Use windows, macos, linux or all.")

    if uninstall:
        console.print(scheduler.uninstall(scheduler.detect_platform(), st.repo))
        return
    if install:
        if target not in ("all", scheduler.detect_platform()):
            raise NightGhostError("--install only works for this machine's platform.")
        message = scheduler.install(scheduler.detect_platform(), st.repo, config.schedule.at, exe)
        ok(message)
        if not state.load_armed(st.repo):
            state.arm(st.repo, at=config.schedule.at, model=config.model.name)
            console.print("[dim]Also armed this repository (scheduled runs only execute while armed).[/dim]")
        return

    platforms = scheduler.PLATFORMS if target == "all" else (target,)
    for plat in platforms:
        console.print(f"[bold cyan]{plat}[/bold cyan]")
        console.print(scheduler.render_for_platform(plat, st.repo, config.schedule.at, exe),
                      markup=False, highlight=False, soft_wrap=True)
    console.print("[dim]Apply with: nightghost schedule --install[/dim]")


def main():
    app()


if __name__ == "__main__":
    main()
