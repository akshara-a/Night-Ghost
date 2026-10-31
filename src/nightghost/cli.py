from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path
from typing import Optional
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from nightghost import __version__
from nightghost.config import DEFAULT_YAML_TEMPLATE, NightGhostConfig
from nightghost.gitmanager import GitManager
from nightghost.pipeline import NightGhostPipeline

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

app = typer.Typer(
    name="nightghost",
    help="Night Ghost: An autonomous local agent that continues your unfinished coding work overnight.",
    add_completion=False,
)
console = Console()


@app.command()
def version():
    """Show Night Ghost version."""
    console.print(f"[bold cyan]Night Ghost[/bold cyan] v{__version__}")


@app.command()
def init(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing configuration"),
):
    """Initialize Night Ghost configuration and workspace in current repository."""
    repo_root = Path.cwd()
    config_file = repo_root / "nightghost.yaml"
    task_dir = repo_root / ".nightghost"
    task_file = task_dir / "task.md"

    if config_file.exists() and not force:
        console.print(f"[yellow]Configuration file already exists:[/yellow] {config_file}")
        console.print("Use [bold]--force[/bold] to overwrite.")
        return

    # Write config
    config_file.write_text(DEFAULT_YAML_TEMPLATE, encoding="utf-8")
    task_dir.mkdir(parents=True, exist_ok=True)

    if not task_file.exists() or force:
        starter_task = (
            "# Unfinished Evening Task\n\n"
            "Add tests for edge cases and clean up any messy parts.\n"
        )
        task_file.write_text(starter_task, encoding="utf-8")

    console.print(
        Panel.fit(
            f"[bold green]Night Ghost Initialized Successfully![/bold green]\n\n"
            f"Created [cyan]{config_file.name}[/cyan]\n"
            f"Created [cyan]{task_file.relative_to(repo_root)}[/cyan]\n\n"
            f"[dim]Next step: Drop a note with `nightghost note \"...\"` then arm with `nightghost sleep`.[/dim]",
            title="Night Ghost",
            border_style="green",
        )
    )


@app.command()
def note(
    task_text: Optional[str] = typer.Argument(
        None, help="The task instructions for tonight's run"
    ),
):
    """Set or display the evening task note."""
    repo_root = Path.cwd()
    config = NightGhostConfig.load(repo_root / "nightghost.yaml")
    task_file = repo_root / config.task.file

    if task_text:
        task_file.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_text(task_text.strip() + "\n", encoding="utf-8")
        console.print(f"[green][OK] Evening note saved to[/green] [cyan]{config.task.file}[/cyan]:")
        console.print(f"[italic]\"{task_text}\"[/italic]")
    else:
        if task_file.is_file():
            content = task_file.read_text(encoding="utf-8").strip()
            console.print(Panel(content, title=f"Task Note ({config.task.file})", border_style="cyan"))
        else:
            console.print(f"[yellow]No task note found at {config.task.file}.[/yellow]")
            console.print("Set one with: [bold]nightghost note \"Your instructions here\"[/bold]")


@app.command()
def run(
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Plan and simulate the pipeline without writing edits or commits"
    ),
    model: Optional[str] = typer.Option(
        None, "--model", "-m", help="Override the LLM model name"
    ),
):
    """Wake and execute the overnight coding pipeline."""
    repo_root = Path.cwd()
    config = NightGhostConfig.load(repo_root / "nightghost.yaml")

    console.print(
        Panel.fit(
            f"[bold cyan]Night Ghost Active[/bold cyan]\n"
            f"Model: [green]{model or config.model.name}[/green] ({config.model.provider})\n"
            f"Mode: {'[yellow]DRY RUN (simulation)[/yellow]' if dry_run else '[bold green]LIVE RUN[/bold green]'}",
            title="Overnight Run",
            border_style="cyan",
        )
    )

    pipeline = NightGhostPipeline(repo_root, config)
    res = pipeline.run(dry_run=dry_run, model_override=model)

    if not res.success:
        console.print(f"[bold red]Pipeline halted with error:[/bold red] {res.error_message}")
        raise typer.Exit(code=1)

    console.print("\n[bold green][OK] Pipeline completed![/bold green]")
    console.print(f"Branch: [cyan]{res.branch_name}[/cyan]")
    console.print(f"Steps Planned: [bold]{len(res.plan.steps)}[/bold]")
    console.print(f"Steps Executed: [bold]{len(res.executed_steps)}[/bold]")
    console.print(f"Elapsed: [dim]{round(res.runtime_seconds, 1)}s[/dim]")
    console.print("\nView the morning report with: [bold cyan]nightghost report[/bold cyan]")


@app.command()
def report(
    date: Optional[str] = typer.Argument(
        None, help="Date of report to display (YYYY-MM-DD), defaults to latest"
    ),
):
    """View the morning report in your terminal."""
    repo_root = Path.cwd()
    reports_dir = repo_root / ".nightghost" / "reports"

    target_file = None
    if date:
        target_file = reports_dir / f"{date}.md"
    else:
        target_file = reports_dir / "latest.md"
        if not target_file.is_file():
            # Check for today's date
            today_str = datetime.date.today().strftime("%Y-%m-%d")
            today_file = reports_dir / f"{today_str}.md"
            if today_file.is_file():
                target_file = today_file

    if not target_file or not target_file.is_file():
        console.print("[yellow]No morning report found yet.[/yellow]")
        console.print("Run Night Ghost first with: [bold]nightghost run[/bold]")
        return

    content = target_file.read_text(encoding="utf-8")
    console.print(Markdown(content))


@app.command()
def discard(
    branch: Optional[str] = typer.Argument(
        None, help="The ghost branch to discard (defaults to current date)"
    ),
    restore: str = typer.Option(
        "main", "--restore", "-r", help="Branch to check out after discarding"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Discard overnight ghost branch and cleanly reset working tree."""
    repo_root = Path.cwd()
    git = GitManager(repo_root)

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    target_branch = branch or f"nightghost/{today_str}"

    if not yes:
        confirm = typer.confirm(f"Are you sure you want to discard branch '{target_branch}'?")
        if not confirm:
            console.print("[dim]Aborted.[/dim]")
            return

    git.discard_branch(target_branch, restore_branch=restore)
    console.print(f"[green][OK] Branch '{target_branch}' discarded. Restored '{restore}'.[/green]")


@app.command()
def status():
    """Check current branch, task note, and safety status."""
    repo_root = Path.cwd()
    config = NightGhostConfig.load(repo_root / "nightghost.yaml")
    git = GitManager(repo_root)

    table = Table(title="Night Ghost Status", border_style="cyan")
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Current Git Branch", git.get_current_branch())
    table.add_row("Uncommitted Changes", "Yes (snapshot armed)" if git.has_uncommitted_changes() else "Clean")
    table.add_row("Scheduled Time", config.schedule.at)
    table.add_row("Configured Model", f"{config.model.name} ({config.model.provider})")
    table.add_row("Test Command", config.commands.test)

    # Check model connectivity
    if config.model.provider == "ollama":
        from nightghost.llm.ollama import OllamaClient
        client = OllamaClient(config.model)
        if client.is_available():
            installed = client.list_models()
            model_base = config.model.name.split(":")[0]
            is_present = any(config.model.name in m or m.startswith(model_base) for m in installed)
            if is_present:
                model_status = "[green]Ready (Installed)[/green]"
            else:
                model_status = f"[yellow]Not downloaded (Run: ollama pull {config.model.name})[/yellow]"
            table.add_row("Ollama Server", "[green]Online[/green]")
            table.add_row("Model Status", model_status)
        else:
            table.add_row("Ollama Server", "[red]Offline (Run: ollama serve)[/red]")
            table.add_row("Model Status", "[red]Unreachable[/red]")
    elif config.model.provider == "mock":
        table.add_row("Model Server", "[cyan]Mock Runner (Offline)[/cyan]")
        table.add_row("Model Status", "[green]Ready[/green]")

    # Check kill switch
    stop_file = repo_root / ".nightghost" / "STOP"
    table.add_row("Kill Switch (.nightghost/STOP)", "[bold red]ENGAGED[/bold red]" if stop_file.exists() else "Disengaged (Ready)")

    console.print(table)


@app.command()
def stop():
    """Engage emergency kill switch (.nightghost/STOP) to halt any running overnight execution."""
    repo_root = Path.cwd()
    stop_file = repo_root / ".nightghost" / "STOP"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text("STOP\n", encoding="utf-8")
    console.print(
        Panel.fit(
            "[bold red]Kill switch engaged![/bold red]\n"
            f"Created [cyan]{stop_file}[/cyan]. Any running or next step will immediately halt.",
            title="Emergency Stop",
            border_style="red",
        )
    )


@app.command()
def sleep():
    """Arm the overnight run and prepare for scheduled execution."""
    repo_root = Path.cwd()
    config = NightGhostConfig.load(repo_root / "nightghost.yaml")
    git = GitManager(repo_root)

    console.print(
        Panel.fit(
            f"[bold cyan]Night Ghost Armed For Sleep[/bold cyan]\n\n"
            f"Scheduled at: [bold]{config.schedule.at}[/bold]\n"
            f"Model: [bold green]{config.model.name}[/bold green]\n"
            f"Task file: [bold]{config.task.file}[/bold]\n\n"
            f"[dim]The runner can be triggered by system cron/launchd/systemd timer at {config.schedule.at}, "
            f"or run directly via `nightghost run`.[/dim]",
            title="Good Night",
            border_style="cyan",
        )
    )


@app.command()
def schedule():
    """Generate cron, systemd timer, or launchd configuration for your OS."""
    repo_root = Path.cwd().resolve()
    config = NightGhostConfig.load(repo_root / "nightghost.yaml")
    hour, minute = config.schedule.at.split(":") if ":" in config.schedule.at else ("1", "0")

    console.print("[bold cyan]Schedule Configurations:[/bold cyan]\n")
    console.print("[bold]1. Crontab entry (Linux / macOS):[/bold]")
    cron_line = f"{int(minute)} {int(hour)} * * * cd \"{repo_root}\" && nightghost run >> .nightghost/run.log 2>&1"
    console.print(f"```bash\n{cron_line}\n```\n")

    console.print("[bold]2. Systemd Service & Timer (Linux):[/bold]")
    systemd_timer = f"""# ~/.config/systemd/user/nightghost.timer
[Unit]
Description=Run Night Ghost Overnight

[Timer]
OnCalendar=*-*-* {config.schedule.at}:00
Persistent=true

[Install]
WantedBy=timers.target
"""
    console.print(f"```ini\n{systemd_timer}\n```")


def main():
    app()


if __name__ == "__main__":
    main()
