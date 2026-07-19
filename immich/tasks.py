"""Invoke tasks for Immich photo and video management."""

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from conjuring.grimoire import get_hostname, lazy_env_variable
from invoke import Context, task

IMMICH_CONTAINER = "immich-db"
IMMICH_DB_USER = "immich"
IMMICH_DB_NAME = "immich"

IMMICH_DIR = Path(__file__).parent


def _compose_file(_c: Context) -> str:
    vessel_dir = os.environ.get("VESSEL_DIR", "~/dev/me/vessel")
    return f"-f {Path(vessel_dir).expanduser()}/immich/compose.yaml"


@task
def immich_setup(c: Context) -> None:
    """Set up Immich: ensure env vars are set and create the library data directory.

    The Postgres database and user are created automatically by the bundled
    immich-db container on first start (via POSTGRES_USER/PASSWORD/DB env vars).
    """
    # Validate required env var before starting anything — fail fast.
    lazy_env_variable("IMMICH_DB_PASSWORD", "Immich PostgreSQL password")

    print("Step 1: Ensuring redis is running...")
    c.run("cd ~/dev/me/vessel/redis && docker compose up -d")

    print("\nStep 2: Creating library data directory...")
    library_dir = (
        Path(lazy_env_variable("VESSEL_DATA_DIR", "Container apps data directory")).expanduser() / "immich" / "library"
    )
    library_dir.mkdir(parents=True, exist_ok=True)
    print(f"  Created: {library_dir}")

    print("\n✅ Immich setup complete!")
    print("\nNext steps:")
    print("  invoke immich-up")
    print("  Open http://localhost:2283 and create admin account")


@task(help={"pull": "Pull latest Immich image before starting"})
def immich_up(c: Context, pull: bool = False, logs: bool = False) -> None:
    """Start Redis, then the Immich stack (immich-db starts automatically)."""
    c.run("vessel redis up")

    cf = _compose_file(c)

    if pull:
        print("Pulling latest Immich images...")
        c.run(f"docker compose {cf} pull")

    print("Starting Immich stack...")
    c.run(f"docker compose {cf} up -d")
    if logs:
        c.run(f"docker compose {cf} logs -f", warn=True, pty=True)


@task(help={"output_dir": "Output directory (default: $BACKUP_DIR/<hostname>/immich)"})
def immich_dump(c: Context, output_dir: str = "") -> None:
    """Dump the Immich database with a timestamp in the file name."""
    datetime_str = datetime.now(UTC).isoformat().replace(":", "-").split(".")[0]

    if output_dir:
        output_path = Path(output_dir).expanduser()
    else:
        host_name = get_hostname()
        output_path = Path(lazy_env_variable("BACKUP_DIR", "Backup directory")).expanduser() / host_name / "immich"

    output_path.mkdir(parents=True, exist_ok=True)

    filename = f"{IMMICH_DB_NAME}_{datetime_str}.sql"
    final_archive = output_path / f"{filename}.tar.gz"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_sql = Path(tmp_dir) / filename
        c.run(f"docker exec {IMMICH_CONTAINER} pg_dump -U {IMMICH_DB_USER} {IMMICH_DB_NAME} > {tmp_sql}")
        c.run(f"tar -czf {final_archive} -C {tmp_dir} {filename}")
    c.run(f"ls -lrth {output_path!s} | tail -n 20", dry=False)


@task(help={"output_dir": "Destination directory (default: $BACKUP_DIR/<hostname>/immich/library)"})
def immich_rsync(c: Context, output_dir: str = "") -> None:
    """Rsync Immich library media to the backup directory."""
    src = (
        Path(lazy_env_variable("VESSEL_DATA_DIR", "Container apps data directory")).expanduser() / "immich" / "library"
    )

    if output_dir:
        dest = Path(output_dir).expanduser()
    else:
        host_name = get_hostname()
        dest = Path(lazy_env_variable("BACKUP_DIR", "Backup directory")).expanduser() / host_name / "immich" / "library"

    dest.mkdir(parents=True, exist_ok=True)
    c.run(f"rsync -av --progress {src}/ {dest}/")


@task(
    help={
        "host": "Hostname whose backup to restore from (required, e.g. FX777YD7FHMac)",
        "input_dir": "Directory containing .sql.tar.gz dumps (default: $BACKUP_DIR/<host>/immich)",
    },
)
def immich_restore(c: Context, host: str = "", input_dir: str = "") -> None:
    """Restore the Immich database from the latest dump.

    Checks whether the DB already exists and prompts before dropping it.
    Idempotent: safe to re-run.
    """
    import sys

    if not host:
        print("ERROR: --host is required (e.g. vessel immich restore --host FX777YD7FHMac)")
        sys.exit(1)

    if input_dir:
        dump_path = Path(input_dir).expanduser()
    else:
        backup_dir = Path(lazy_env_variable("BACKUP_DIR", "Backup directory")).expanduser()
        dump_path = backup_dir / host / "immich"

    archives = sorted(dump_path.glob("*.sql.tar.gz"), reverse=True)
    if not archives:
        print(f"ERROR: no .sql.tar.gz files found in {dump_path}")
        sys.exit(1)

    latest = archives[0]
    print(f"  dump  {latest}")

    # Check if DB already exists
    result = c.run(
        f"docker exec {IMMICH_CONTAINER} psql -U {IMMICH_DB_USER} -lqt",
        warn=True,
        hide=True,
    )
    db_exists = result.ok and IMMICH_DB_NAME in result.stdout

    if db_exists:
        answer = input(f"  Database '{IMMICH_DB_NAME}' exists - drop and restore? [y/n/q] ").strip().lower()
        if answer == "q":
            print("  quit")
            sys.exit(0)
        if answer != "y":
            print("  skip  restore cancelled")
            return
        # Terminate all connections first (values are module-level constants, not user input)
        terminate_sql = (
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity"  # noqa: S608
            f" WHERE datname='{IMMICH_DB_NAME}' AND pid <> pg_backend_pid();"
        )
        c.run(f'docker exec {IMMICH_CONTAINER} psql -U {IMMICH_DB_USER} postgres -c "{terminate_sql}"')
        c.run(
            f"docker exec {IMMICH_CONTAINER} psql -U {IMMICH_DB_USER} postgres"
            f" -c 'DROP DATABASE IF EXISTS {IMMICH_DB_NAME};'"
        )
        c.run(
            f"docker exec {IMMICH_CONTAINER} psql -U {IMMICH_DB_USER} postgres"
            f" -c 'CREATE DATABASE {IMMICH_DB_NAME} OWNER {IMMICH_DB_USER};'"
        )
        print(f"  drop  {IMMICH_DB_NAME}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        c.run(f"tar -xzf {latest} -C {tmp_dir}")
        sql_files = sorted(Path(tmp_dir).glob("*.sql"), reverse=True)
        if not sql_files:
            print(f"ERROR: no .sql file found inside {latest.name}")
            sys.exit(1)
        sql = sql_files[0]
        print(f"  sql   {sql.name}")
        c.run(f"docker exec -i {IMMICH_CONTAINER} psql -U {IMMICH_DB_USER} {IMMICH_DB_NAME} < {sql}")

    print(f"  done  {IMMICH_DB_NAME} restored from {latest.name}")


@task
def browse(c: Context) -> None:
    """Browse Immich library."""
    c.run("open http://localhost:2283")
