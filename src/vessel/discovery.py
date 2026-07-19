"""Discover compose dirs and build Click groups for each app."""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import click
import typer
from conjuring import invoke_context, invoke_to_click
from conjuring.grimoire import run_command
from invoke.exceptions import UnexpectedExit

if TYPE_CHECKING:
    import types


_COMPOSE_FILENAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
_SIGINT_EXIT_CODE = 130  # Ctrl+C exit code (128 + SIGINT signal 2)


def _compose_roots() -> list[Path]:
    """Return dirs to search. Uses VESSEL_ROOTS env var if set, else repo root."""
    raw = os.environ.get("VESSEL_ROOTS", "")
    if raw.strip():
        return [Path(p).expanduser() for p in raw.split(":") if p.strip()]
    # src/vessel/discovery.py → src/vessel/ → src/ → repo root
    return [Path(__file__).parent.parent.parent]


def _find_compose_file(directory: Path) -> Path | None:
    """Return the first compose file found in directory, or None."""
    return next((directory / name for name in _COMPOSE_FILENAMES if (directory / name).exists()), None)


def _find_compose_dirs(roots: list[Path]) -> list[Path]:
    """Walk one level deep in each root, return dirs containing a compose file, sorted globally by name."""
    candidates = [
        candidate
        for root in roots
        if root.is_dir()
        for candidate in root.iterdir()
        if candidate.is_dir() and _find_compose_file(candidate)
    ]
    return sorted(candidates, key=lambda p: p.name)


def _load_module(path: Path) -> types.ModuleType:
    """Import a Python file by absolute path as an anonymous module."""
    # Qualify with parent dir name to avoid stem collisions (e.g. two tasks.py files)
    module_name = f"vessel._dynamic.{path.parent.name}.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod  # type: ignore[return-value]


def _extra_click_commands(app_dir: Path) -> list[click.Command]:
    """Load extra commands from cli.py or tasks.py next to compose.yaml.

    Priority: cli.py > tasks.py. Within cli.py: Typer app > @task functions.
    """
    from vessel.main import get_dry

    def ctx_factory() -> object:
        return invoke_context(dry_run=get_dry())

    for filename in ("cli.py", "tasks.py"):
        path = app_dir / filename
        if not path.exists():
            continue

        try:
            mod = _load_module(path)
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: could not load {path}: {exc}", file=sys.stderr)
            return []

        if filename == "cli.py":
            cli_app = getattr(mod, "app", None)
            if isinstance(cli_app, typer.Typer):
                # Typer app convention: extract its compiled Click commands
                import typer.main as _typer_main

                compiled: click.Group = _typer_main.get_command(cli_app)  # type: ignore[assignment]
                return list(compiled.commands.values())

        # Invoke @task convention (cli.py fallback or tasks.py)
        from invoke import Task

        app_prefix = app_dir.name + "_"
        tasks = [obj for obj in vars(mod).values() if isinstance(obj, Task)]
        cmds = [invoke_to_click(t, ctx_factory) for t in tasks]
        # Strip redundant app-dir prefix from command names (e.g. zammad_setup → setup)
        for cmd in cmds:
            if cmd.name and cmd.name.startswith(app_prefix):
                cmd.name = cmd.name[len(app_prefix) :]
        return cmds

    return []


def _register_compose_commands(group: click.Group, compose_file: str) -> None:
    """Register the generic Docker Compose commands on a Click group."""

    def _ctx() -> object:
        from vessel.main import get_dry

        return invoke_context(dry_run=get_dry())

    @group.command()
    @click.option("--pull", is_flag=True, default=False, help="Pull images before starting.")
    def up(pull: bool) -> None:
        """Start containers in the background."""
        c = _ctx()
        if pull:
            run_command(c, "docker compose -f", compose_file, "pull")
        run_command(c, "docker compose -f", compose_file, "up -d")

    @group.command()
    def down() -> None:
        """Stop and remove containers."""
        run_command(_ctx(), "docker compose -f", compose_file, "down")

    @group.command()
    @click.option("--follow/--no-follow", default=True, help="Follow log output.")
    def logs(follow: bool) -> None:
        """Show container logs."""
        try:
            run_command(_ctx(), "docker compose -f", compose_file, "logs", "-f" if follow else "", pty=True, warn=True)
        except UnexpectedExit as e:
            if e.result.exited != _SIGINT_EXIT_CODE:
                raise

    @group.command()
    def ps() -> None:
        """List containers."""
        run_command(_ctx(), "docker compose -f", compose_file, "ps")

    @group.command()
    def stop() -> None:
        """Stop containers without removing them."""
        run_command(_ctx(), "docker compose -f", compose_file, "stop")


def _build_click_group(app_dir: Path, extra_commands: list[click.Command] | None = None) -> click.Group:
    """Build a Click group for app_dir with generic compose commands."""
    compose_file = str(_find_compose_file(app_dir))
    root_label = app_dir.parent.name

    @click.group(name=app_dir.name, help=f"Manage {app_dir.name}  [{root_label}]", invoke_without_command=True)
    @click.pass_context
    def group(ctx: click.Context) -> None:
        if ctx.invoked_subcommand is None:
            click.echo(ctx.get_help())

    _register_compose_commands(group, compose_file)

    for cmd in extra_commands or []:
        group.add_command(cmd)

    return group


_GetCommand = Callable[[typer.Typer], click.BaseCommand]


def _make_patched_get_command(
    root_app: typer.Typer,
    groups: list[click.Group],
    original: _GetCommand,
) -> _GetCommand:
    """Return a patched get_command that injects discovered groups into root_app."""

    def _patched(typer_instance: typer.Typer) -> click.BaseCommand:
        click_cmd = original(typer_instance)
        # Typer 0.27 uses its own Click-compatible TyperGroup, which is no
        # longer a subclass of click.Group. Both group implementations expose
        # add_command() and commands, so rely on that interface instead.
        if typer_instance is root_app and hasattr(click_cmd, "add_command"):
            for grp in groups:
                if grp.name not in click_cmd.commands:
                    click_cmd.add_command(grp)
        return click_cmd

    setattr(_patched, "_vessel_patched", True)  # noqa: B010
    return _patched


def discover_and_mount(root_app: typer.Typer) -> None:
    """Discover compose dirs and attach Click groups to root_app's Click group.

    Called at import time in main.py. Typer rebuilds its Click group on every
    get_command() call, so we patch typer.main.get_command to inject discovered
    groups into the freshly-built Click group each time it is compiled.
    """
    roots = _compose_roots()
    groups = [_build_click_group(app_dir, _extra_click_commands(app_dir)) for app_dir in _find_compose_dirs(roots)]

    if not groups:
        return

    import typer.main as typer_main

    if getattr(typer_main.get_command, "_vessel_patched", False):
        return

    typer_main.get_command = _make_patched_get_command(root_app, groups, typer_main.get_command)  # type: ignore[assignment]
