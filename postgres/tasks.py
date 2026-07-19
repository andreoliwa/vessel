"""Invoke tasks for PostgreSQL database management."""

import socket
from datetime import UTC, datetime
from pathlib import Path

from conjuring.grimoire import ask_yes_no, lazy_env_variable, print_error, run_command
from invoke import Context, Exit, task

DB_USER = "postgres"
POSTGRES_VERSION = 17
POSTGRES_ENV = "POSTGRES_PASSWORD"


def _backup_dir() -> Path:
    return Path(lazy_env_variable("BACKUP_DIR", "Backup directory")).expanduser()


@task(
    help={
        "database": "Database name",
        "version": f"PostgreSQL version (default: {POSTGRES_VERSION})",
        "psql": "Use psql instead of pgcli",
        "command": "Run a SQL command (only works with psql, ignored on pgcli)",
    }
)
def connect(
    c: Context,
    database: str = "postgres",
    version: int = POSTGRES_VERSION,
    psql: bool = False,
    command: str = "",
) -> None:
    """Connect to the containerised PostgreSQL database using pgcli."""
    db_user = DB_USER
    db_password = lazy_env_variable(POSTGRES_ENV.upper(), "PostgreSQL password")
    if psql:
        if command:
            run_command(
                c,
                f"docker exec postgres{version} psql -U {db_user} --csv --tuples-only",
                f'--command="{command}"',
                database,
            )
        else:
            run_command(c, f"docker exec -it postgres{version} psql -U {db_user}", database, interactive=True)
    else:
        if command:
            print_error(f"Use --psql to run this command: {command}")
            raise Exit(code=1)

        port = f"77{version}"
        run_command(c, f"pgcli postgresql://{db_user}:{db_password}@localhost:{port}/{database}", interactive=True)


@task(
    help={
        "version": f"PostgreSQL version (default: {POSTGRES_VERSION})",
    }
)
def ls(c: Context, version: int = POSTGRES_VERSION) -> None:
    """List databases using psql."""
    db_user = DB_USER
    command = (
        "SELECT datname AS database_name FROM pg_database"
        " WHERE datname NOT IN ('postgres') AND datname NOT LIKE 'template%';"
    )
    run_command(
        c,
        f"docker exec postgres{version} psql -U {db_user} --csv --tuples-only",
        f'--command="{command}"',
        "postgres",
    )


@task(
    help={
        "database": "Database name",
        "version": f"PostgreSQL version (default: {POSTGRES_VERSION})",
        "output_dir": "Output directory for the dump file (default: $BACKUP_DIR/<hostname>/postgres<version>)",
    }
)
def dump(c: Context, database: str, version: int = POSTGRES_VERSION, output_dir: str = "") -> None:
    """Dump a single database (with date/time on the file name)."""
    datetime_str = datetime.now(UTC).isoformat().replace(":", "-").split(".")[0]

    output_path: Path
    if not output_dir:
        host_name = socket.gethostname().replace(".local", "")
        output_path = _backup_dir() / host_name / f"postgres{version}"
    else:
        output_path = Path(output_dir).expanduser()

    output_path.mkdir(exist_ok=True, parents=True)

    full_dump_path = output_path / f"{database}_{datetime_str}.sql"
    c.run(f"docker exec postgres{version} pg_dump -U postgres {database} > {full_dump_path}")
    c.run(f"ls -lrth {output_path!s} | tail -n 20", dry=False)


@task(
    help={
        "file": "Path to the dump file (.sql or .sql.gz)",
        "database": "Target database name",
        "role": "Database role/user to grant privileges to",
        "version": f"PostgreSQL version (default: {POSTGRES_VERSION})",
    }
)
def restore(
    c: Context,
    file: str,
    database: str,
    role: str,
    version: int = POSTGRES_VERSION,
) -> None:
    r"""Restore a database dump (.sql or .sql.gz) into a PostgreSQL container.

    Drops and recreates the target database, then restores from the dump file.
    The dump file must be accessible on the host; if it is not already under
    $BACKUP_DIR it will be copied there so the container can reach it.

    Example:
        vessel postgres restore --file ~/redmine_2026-03-15-20-45-26.sql.gz \
            --database redmine_migration --role redmine --version 17

    """
    dump_path = Path(file).expanduser().resolve()
    if not dump_path.exists():
        print_error(f"Dump file not found: {dump_path}")
        raise Exit(code=1)

    # Fail fast if the env var is not set
    backup_volume = _backup_dir()

    container = f"postgres{version}"

    # Check if the database already exists (S608: not SQL injection — this is a CLI command)
    pg_query = f"SELECT 1 FROM pg_database WHERE datname = '{database}';"  # noqa: S608
    check_cmd = f'docker exec {container} psql -U {DB_USER} --tuples-only --no-align -c "{pg_query}"'
    result = c.run(check_cmd, hide=True, warn=True)
    db_exists = result and result.stdout.strip() == "1"

    if db_exists and not ask_yes_no(
        f"Database '{database}' already exists on {container}.\n"
        "It will be DROPPED and recreated. All data will be lost.\n"
        "Continue?"
    ):
        print_error("Aborted.")
        raise Exit(code=0)

    # Copy dump into the backup volume if it's not already there
    if not str(dump_path).startswith(str(backup_volume)):
        dest = backup_volume / dump_path.name
        print(f"Copying {dump_path} → {dest}")
        c.run(f"cp {dump_path} {dest}")
        container_dump_path = f"/var/backups/{dump_path.name}"
    else:
        # Already inside $BACKUP_DIR — compute relative path inside /var/backups/
        relative = dump_path.relative_to(backup_volume)
        container_dump_path = f"/var/backups/{relative}"

    # Ensure the role exists before granting privileges
    c.run(f'docker exec {container} psql -U {DB_USER} -c "CREATE ROLE {role} LOGIN;" ', warn=True)

    # Drop and recreate database
    print(f"Dropping and recreating database '{database}' on {container}...")
    c.run(f'docker exec {container} psql -U {DB_USER} -c "DROP DATABASE IF EXISTS {database};"')
    c.run(f'docker exec {container} psql -U {DB_USER} -c "CREATE DATABASE {database};"')
    c.run(f'docker exec {container} psql -U {DB_USER} -c "GRANT ALL PRIVILEGES ON DATABASE {database} TO {role};"')

    # Restore the dump
    print(f"Restoring {dump_path.name} into '{database}'...")
    is_gzip = dump_path.suffix == ".gz"
    if is_gzip:
        restore_cmd = f"docker exec -i {container} sh -c 'zcat {container_dump_path} | psql -U {DB_USER} -d {database}'"
    else:
        restore_cmd = f"docker exec -i {container} sh -c 'psql -U {DB_USER} -d {database} -f {container_dump_path}'"

    c.run(restore_cmd)
    print(f"✅ Restore of '{database}' on {container} complete.")
