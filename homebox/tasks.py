# Copyright (c) 2026 W. Augusto Andreoli
"""Invoke tasks for HomeBox home inventory."""

from invoke import Context, task


@task
def browse(c: Context) -> None:
    """Browse HomeBox."""
    c.run("open http://localhost:8009")
