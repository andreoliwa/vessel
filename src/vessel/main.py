"""Root `vessel` CLI app."""

from __future__ import annotations

import typer

app = typer.Typer(no_args_is_help=True, rich_markup_mode=None, pretty_exceptions_enable=False)
_dry_run: bool = False


@app.callback()
def main(
    ctx: typer.Context,
    dry: bool = typer.Option(False, "--dry", "-R", help="Echo commands instead of running."),  # noqa: FBT003
) -> None:
    """Dynamic Docker Compose CLI for vessel with Click commands built from Invoke tasks."""
    global _dry_run  # noqa: PLW0603
    _dry_run = dry
    ctx.ensure_object(dict)
    ctx.obj["dry"] = dry


def get_dry() -> bool:
    """Return the current dry-run flag (set by the root callback)."""
    return _dry_run


from vessel.discovery import discover_and_mount  # noqa: E402

discover_and_mount(app)
