"""Invoke tasks for Zammad helpdesk/ticket system."""

from __future__ import annotations

import json
import logging
import os
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg2.extensions

import tomllib

from conjuring.grimoire import ask_yes_no, lazy_env_variable, print_error, print_warning
from invoke import Context, Exit, task

from vessel.zammad_client import ZammadAPI, ZammadAPIError

# --- Constants ---

ZAMMAD_DIR = Path(__file__).parent
MAP_FILE = ZAMMAD_DIR / "migration_map.json"
ERROR_LOG = ZAMMAD_DIR / "migration_errors.log"
TOML_FILE = ZAMMAD_DIR / "zammad.toml"

# Key used in TOML [states] entries to specify the target Zammad state type
TOML_STATE_TYPE_KEY = "zammad_type"

# Map key used in migration_map["groups"] for the import group
MIGRATION_MAP_GROUP_KEY = "redmine_import"

# Docker exec prefixes (avoids repetition across c.run() calls)
_PG17 = "docker exec postgres17 psql -U postgres"
_RAILS = "docker exec zammad-railsserver bundle exec"

# Sentinel key for the synthetic Redmine status custom field.
_REDMINE_STATUS_CF_KEY = "__redmine_status__"
_REDMINE_STATUS_CF_NAME = "redmine_status"


class _StateType(StrEnum):
    """Zammad built-in ticket state types. Inherits str so members compare/format as plain strings."""

    OPEN = "open"
    CLOSED = "closed"
    PENDING_REMINDER = "pending reminder"
    PENDING_ACTION = "pending action"


class _Priority(StrEnum):
    """Zammad built-in ticket priorities. Inherits str so members compare/format as plain strings."""

    LOW = "1 low"
    NORMAL = "2 normal"
    HIGH = "3 high"


PENDING_STATE_TYPES = {_StateType.PENDING_REMINDER.value, _StateType.PENDING_ACTION.value}


def _compose_file(_c: Context) -> str:
    vessel_dir = os.environ.get("VESSEL_DIR", "~/dev/me/vessel")
    return f"-f {Path(vessel_dir).expanduser()}/zammad/compose.yaml"


def _load_toml() -> dict:
    """Load zammad.toml; raise with a helpful message if missing."""
    if not TOML_FILE.exists():
        msg = f"{TOML_FILE} not found. Copy zammad.toml.example to zammad.toml and fill in your values."
        raise FileNotFoundError(msg)
    with TOML_FILE.open("rb") as f:
        return tomllib.load(f)


# --- Setup / lifecycle tasks ---


@task
def zammad_setup(c: Context) -> None:
    """Set up Zammad: create PostgreSQL 17 database and user."""
    db_pass = lazy_env_variable("ZAMMAD_DB_PASSWORD", "Zammad PostgreSQL password")

    print("Step 1: Starting PostgreSQL 17...")
    c.run("cd postgres && docker compose up -d postgres17")

    print("\nStep 2: Creating Zammad database and user...")
    c.run(f'{_PG17} -c "CREATE DATABASE zammad;"')
    c.run(f"{_PG17} -c \"CREATE USER zammad WITH PASSWORD '{db_pass}' CREATEDB;\"")
    c.run(f'{_PG17} -c "GRANT ALL PRIVILEGES ON DATABASE zammad TO zammad;"')
    c.run(f'{_PG17} -d zammad -c "GRANT ALL ON SCHEMA public TO zammad;"')

    print("\n✅ Zammad database setup complete!")
    print("\nNext steps:")
    print("  1. cd redis && docker compose up -d")
    print("  2. invoke zammad-up")
    print("  3. Open http://localhost:8008")


@task(help={"pull": "Pull latest Zammad images before starting"})
def zammad_up(c: Context, pull: bool = False) -> None:
    """Start the Zammad stack (requires postgres17 and redis running)."""
    cf = _compose_file(c)

    if pull:
        print("Pulling latest Zammad images...")
        c.run(f"docker compose {cf} pull")

    print("Starting Zammad stack...")
    c.run(f"docker compose {cf} up -d")
    c.run(f"docker compose {cf} logs -f")


@task
def zammad_down(c: Context) -> None:
    """Stop the Zammad stack."""
    cf = _compose_file(c)
    print("Stopping Zammad stack...")
    c.run(f"docker compose {cf} down")


def _psql_delete(sql: str, dry_run: bool) -> bool:
    """Execute a SQL statement via docker exec on postgres17/zammad. No-op in dry-run mode.

    Returns True on success, False on failure.
    """
    import subprocess

    if dry_run:
        print(f"  [DRY-RUN] {sql}")
        return True
    result = subprocess.run(
        ["docker", "exec", "postgres17", "psql", "-U", "postgres", "-d", "zammad", "-c", sql],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print_error(f"DB error: {result.stderr.strip()}")
        return False
    return True


def _wipe_via_db(migration_map: dict, dry_run: bool) -> None:
    """Delete all migrated data directly from the Zammad database, in FK-safe order."""
    # Collect IDs from migration_map for targeted DELETE statements.
    ticket_ids = list(migration_map.get("tickets", {}).values())
    # article IDs: stored as values in migration_map["articles"] (includes _first sentinel keys)
    article_ids = list(migration_map.get("articles", {}).values())
    # links are stored as {key: True}, no DB IDs — they live in the links table keyed by ticket IDs
    overview_ids = list(migration_map.get("overviews", {}).values())
    # users + customers both map to Zammad user IDs
    user_ids = list(
        {
            *migration_map.get("users", {}).values(),
            *migration_map.get("customers", {}).values(),
        }
    )
    org_ids = list(migration_map.get("organizations", {}).values())
    group_ids = list(migration_map.get("groups", {}).values())
    # custom_fields values are dicts {"zammad_name": ..., "id": ...}
    cf_ids = [v["id"] for v in migration_map.get("custom_fields", {}).values() if isinstance(v, dict) and "id" in v]

    def _ids_sql(ids: list) -> str:
        return "ARRAY[" + ",".join(str(int(i)) for i in ids) + "]"

    ok = True  # tracks whether all statements succeeded

    # All SQL strings below use only integer IDs from migration_map.json — noqa: S608 is safe here.
    if article_ids:
        print(f"  Deleting {len(article_ids)} articles...")
        ok &= _psql_delete(f"DELETE FROM ticket_articles WHERE id = ANY({_ids_sql(article_ids)});", dry_run)  # noqa: S608

    if ticket_ids:
        print(f"  Deleting {len(ticket_ids)} tickets (and their dependent rows)...")
        ids = _ids_sql(ticket_ids)
        # Delete FK-dependent rows first, then tickets themselves.
        # mentions uses a polymorphic association (mentionable_type/id), not a direct ticket_id FK.
        ok &= _psql_delete(
            f"DELETE FROM mentions WHERE mentionable_type = 'Ticket' AND mentionable_id = ANY({ids});",  # noqa: S608
            dry_run,
        )
        ok &= _psql_delete(f"DELETE FROM ticket_time_accountings WHERE ticket_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM ticket_daily_event_locks WHERE ticket_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM ticket_shared_draft_zooms WHERE ticket_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM checklist_items WHERE ticket_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(
            f"DELETE FROM links WHERE link_object_source_value = ANY({ids}) OR link_object_target_value = ANY({ids});",  # noqa: S608
            dry_run,
        )
        ok &= _psql_delete(f"DELETE FROM tags WHERE o_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM activity_streams WHERE o_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM tickets WHERE id = ANY({ids});", dry_run)  # noqa: S608

    if overview_ids:
        print(f"  Deleting {len(overview_ids)} overviews...")
        ids = _ids_sql(overview_ids)
        ok &= _psql_delete(f"DELETE FROM overviews_roles WHERE overview_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM overviews_users WHERE overview_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM overviews_groups WHERE overview_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM overviews WHERE id = ANY({ids});", dry_run)  # noqa: S608

    if user_ids:
        print(f"  Deleting {len(user_ids)} users...")
        ids = _ids_sql(user_ids)
        ok &= _psql_delete(
            f"DELETE FROM online_notifications WHERE user_id = ANY({ids}) OR created_by_id = ANY({ids});",  # noqa: S608
            dry_run,
        )
        ok &= _psql_delete(f"DELETE FROM taskbars WHERE user_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM recent_views WHERE created_by_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM recent_closes WHERE user_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM user_devices WHERE user_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM user_overview_sortings WHERE user_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM user_two_factor_preferences WHERE user_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM cti_caller_ids WHERE user_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM groups_users WHERE user_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM roles_users WHERE user_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM organizations_users WHERE user_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM users WHERE id = ANY({ids});", dry_run)  # noqa: S608

    if org_ids:
        print(f"  Deleting {len(org_ids)} organizations...")
        ids = _ids_sql(org_ids)
        # Null out organization_id on any remaining users before deleting orgs.
        ok &= _psql_delete(f"UPDATE users SET organization_id = NULL WHERE organization_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM organizations WHERE id = ANY({ids});", dry_run)  # noqa: S608

    if group_ids:
        print(f"  Deleting {len(group_ids)} groups...")
        ids = _ids_sql(group_ids)
        # Null out group_id on any remaining tickets before deleting groups.
        ok &= _psql_delete(f"UPDATE tickets SET group_id = NULL WHERE group_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM groups_users WHERE group_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM groups_macros WHERE group_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM groups_text_modules WHERE group_id = ANY({ids});", dry_run)  # noqa: S608
        ok &= _psql_delete(f"DELETE FROM groups WHERE id = ANY({ids});", dry_run)  # noqa: S608

    if cf_ids:
        print(f"  Deleting {len(cf_ids)} custom fields...")
        ok &= _psql_delete(f"DELETE FROM object_manager_attributes WHERE id = ANY({_ids_sql(cf_ids)});", dry_run)  # noqa: S608

    if not dry_run:
        if ok:
            MAP_FILE.unlink()
            print("\n✅ migration_map.json deleted.")
            print("✅ Wipe complete. You can now re-run invoke zammad-migrate.")
        else:
            print_warning("\n⚠️  Some deletions failed — migration_map.json kept so you can retry.")


@task(help={"drop": "Drop the zammad database entirely and return (skips per-entity cleanup)"})
def zammad_wipe(c: Context, drop: bool = False) -> None:
    """Delete all data created by the Redmine import directly via the database (fast)."""
    dry_run: bool = c.config.run.dry

    if drop:
        if dry_run:
            print("[DRY-RUN] Would stop Zammad stack and drop database and role 'zammad' on postgres17.")
            return
        if not ask_yes_no(
            "⚠️ This is a destructive command and cannot be undone.\n"
            "   The Zammad Docker stack will be stopped (with 'docker compose down')\n"
            "   and the database will be deleted.\n"
            "   Are you sure?"
        ):
            return
        zammad_down(c)
        print("Dropping database and role 'zammad'...")
        c.run(f'{_PG17} -c "DROP DATABASE IF EXISTS zammad;"')
        c.run(f'{_PG17} -c "DROP ROLE IF EXISTS zammad;"')
        if MAP_FILE.exists():
            MAP_FILE.unlink()
        print("✅ Database and role dropped.")
        print("   Run 'invoke zammad-setup' to recreate it, then 'invoke zammad-up'.")
        return

    if not MAP_FILE.exists():
        print("No migration_map.json found — nothing to wipe.")
        return

    migration_map = _load_migration_map()
    _wipe_via_db(migration_map, dry_run)


@task
def zammad_reindex(c: Context) -> None:
    """Rebuild the Zammad Elasticsearch search index."""
    _import_mode_off(c)
    print("Rebuilding Zammad Elasticsearch index...")
    c.run(f"{_RAILS} rake zammad:searchindex:rebuild")
    print("✅ Reindex complete. Search results may take a few minutes to reflect all tickets.")


@task
def zammad_fetch_emails(c: Context) -> None:
    """Force Zammad to fetch emails from all configured channels immediately."""
    print("Fetching emails from all configured channels...")
    c.run(f'{_RAILS} rails r "Channel.fetch"')
    print("✅ Email fetch complete.")


def _zammad_db_connect(db_pass: str = "") -> psycopg2.extensions.connection:
    """Return a psycopg2 connection to the Zammad database on postgres17."""
    import psycopg2

    if not db_pass:
        db_pass = os.environ.get("ZAMMAD_DB_PASSWORD", os.environ.get("POSTGRES_PASSWORD", ""))
    return psycopg2.connect(host="localhost", port=5433, dbname="zammad", user="zammad", password=db_pass)


def _search_zammad_users(conn, term: str) -> list[dict]:
    """Return users whose login, email, or full name contains *term* (case-insensitive)."""
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """
            SELECT id, login, email, firstname, lastname, preferences
            FROM users
            WHERE login ILIKE %s
               OR email ILIKE %s
               OR (firstname || ' ' || lastname) ILIKE %s
            ORDER BY id
            """,
            (f"%{term}%", f"%{term}%", f"%{term}%"),
        )
        return [dict(r) for r in cur.fetchall()]


@task(
    help={
        "from_user": "Partial name, email or login — must match exactly one user",
        "to_users": "Partial name, email or login — may match multiple users",
        "db_pass": "Zammad DB password (or ZAMMAD_DB_PASSWORD / POSTGRES_PASSWORD env var)",
    }
)
def zammad_copy_email_settings(
    _c: Context,
    from_user: str = "",
    to_users: str = "",
    db_pass: str = "",
) -> None:
    """Copy email notification settings from one Zammad user to one or more others.

    Reads notification_config from the --from user's preferences and writes it into
    the preferences of every user matched by --to, directly in the Zammad PostgreSQL DB.
    """
    import yaml

    if not from_user or not to_users:
        print_error("Both --from-user and --to-users are required.")
        raise Exit(code=1)

    conn = _zammad_db_connect(db_pass)
    try:
        # --- resolve source user (must be exactly one) ---
        sources = _search_zammad_users(conn, from_user)
        if len(sources) == 0:
            print_error(f"--from-user '{from_user}': no users matched.")
            raise Exit(code=1)
        if len(sources) > 1:
            print_error(f"--from-user '{from_user}': matched {len(sources)} users (must match exactly one):")
            for u in sources:
                print_error(
                    f"  id={u['id']}  login={u['login']}  email={u['email']}  name={u['firstname']} {u['lastname']}"
                )
            raise Exit(code=1)

        source = sources[0]
        src_prefs = yaml.safe_load(source["preferences"] or "") or {}
        notification_config = src_prefs.get("notification_config")
        if not notification_config:
            print_error(f"--from-user '{source['login']}' (id={source['id']}) has no notification_config — aborting.")
            raise Exit(code=1)

        print(
            f"Source user: {source['firstname']} {source['lastname']}  login={source['login']}  email={source['email']}"
        )
        print("\nEmail notification settings to copy:")
        print(yaml.dump({"notification_config": notification_config}, default_flow_style=False).rstrip())

        # --- resolve target users (one or more) ---
        targets = _search_zammad_users(conn, to_users)
        if not targets:
            print_error(f"--to-users '{to_users}': no users matched.")
            raise Exit(code=1)

        print(f"\nTarget users ({len(targets)} matched):")
        for u in targets:
            print(f"  id={u['id']}  login={u['login']}  email={u['email']}  name={u['firstname']} {u['lastname']}")

        # --- apply notification_config to each target ---
        updated = 0
        with conn.cursor() as cur:
            for target in targets:
                tgt_prefs = yaml.safe_load(target["preferences"] or "") or {}
                tgt_prefs["notification_config"] = notification_config
                new_prefs = yaml.dump(tgt_prefs, default_flow_style=False)
                cur.execute(
                    "UPDATE users SET preferences = %s, updated_at = NOW() WHERE id = %s",
                    (new_prefs, target["id"]),
                )
                updated += 1

        conn.commit()
        print(f"\n✅ notification_config copied to {updated} user(s).")
    finally:
        conn.close()


def _import_mode_on(c: Context) -> None:
    print("Enabling import mode (backdates timestamps, suppresses notifications)...")
    c.run(f"{_RAILS} rails r \"Setting.set('import_mode', true)\"")
    c.run(f"{_RAILS} rails r \"Setting.set('system_init_done', false)\"")


def _import_mode_off(c: Context) -> None:
    print("Disabling import mode...")
    c.run(f"{_RAILS} rails r \"Setting.set('import_mode', false)\"")
    c.run(f"{_RAILS} rails r \"Setting.set('system_init_done', true)\"")
    c.run(f'{_RAILS} rails r "Rails.cache.clear"')


# --- Migration internals ---


def _load_migration_map() -> dict:
    if MAP_FILE.exists():
        return json.loads(MAP_FILE.read_text())
    return {
        "users": {},
        "customers": {},
        "groups": {},
        "organizations": {},
        "tickets": {},
        "articles": {},
        "custom_fields": {},
        "states": {},
        "links": {},
        "overviews": {},
        "tags": {},
    }


def _save_migration_map(migration_map: dict, dry_run: bool = False) -> None:
    if not dry_run:
        MAP_FILE.write_text(json.dumps(migration_map, indent=2))


class _CountingHandler(logging.Handler):
    """Counts ERROR (and above) log records emitted through it."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.count = 0

    def emit(self, _record: logging.LogRecord) -> None:
        self.count += 1


class _PrintErrorHandler(logging.Handler):
    """Forwards each log record to print_error() for consistent CLI styling."""

    def emit(self, record: logging.LogRecord) -> None:
        print_error(self.format(record))


def _md_to_html(text: str) -> str:
    import markdown

    return markdown.markdown(text, extensions=["extra", "nl2br", "sane_lists"])


def _issue_body(description: str | None, issue_id: int, redmine_base_url: str) -> str:
    """Return the HTML body for a ticket: optional Redmine link header + converted description."""
    body = _md_to_html(description or "(no description)")
    if redmine_base_url:
        url = f"{redmine_base_url}/issues/{issue_id}"
        link = f'<p><a href="{url}">{url}</a></p>'
        body = link + "\n" + body
    return body


# ANSI color codes for log output: yellow for WARNING, red for ERROR/CRITICAL.
_LOG_LEVEL_COLORS = {
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[31m",
}
_LOG_COLOR_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        color = _LOG_LEVEL_COLORS.get(record.levelno, "")
        return color + super().format(record) + (_LOG_COLOR_RESET if color else "")


def _setup_error_logging() -> tuple[logging.Logger, _CountingHandler]:
    logger = logging.getLogger("migration_errors")
    logger.setLevel(logging.WARNING)
    counter = _CountingHandler()
    if not logger.handlers:
        file_handler = logging.FileHandler(ERROR_LOG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        stderr_handler = _PrintErrorHandler()
        stderr_handler.setFormatter(_ColorFormatter("%(message)s"))
        logger.addHandler(file_handler)
        logger.addHandler(stderr_handler)
        logger.addHandler(counter)
    else:
        # Re-attach counter on subsequent calls (e.g. re-used logger instance).
        logger.addHandler(counter)
    return logger, counter


def _sanitize_field_name(redmine_name: str) -> str:
    from slugify import slugify

    return "redmine_" + slugify(redmine_name, separator="_")


def _overview_link(name: str) -> str:
    from slugify import slugify

    return slugify(name)


def _connect_redmine_db(host: str, port: int, dbname: str, user: str, password: str) -> psycopg2.extensions.connection:
    import psycopg2

    return psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)


def _tags_table_exists(conn) -> str | None:
    """Return the tags table name if a supported tags plugin is installed, else None."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_name IN ('tags', 'additional_tags')
              AND table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY table_name LIMIT 1
        """)
        row = cur.fetchone()
        return row[0] if row else None


def _read_redmine_users(conn) -> list:
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT u.id, u.login, u.firstname, u.lastname, u.status,
                   ea.address AS mail
            FROM users u
            LEFT JOIN email_addresses ea
                   ON ea.user_id = u.id AND ea.is_default = true
            WHERE u.type = 'User'
            ORDER BY u.id
        """)
        return cur.fetchall()


def _read_redmine_custom_fields(conn) -> list:
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT id, name, field_format, possible_values, is_required, default_value
            FROM custom_fields
            WHERE type = 'IssueCustomField'
            ORDER BY id
        """)
        return cur.fetchall()


def _read_redmine_trackers(conn) -> list:
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT id, name FROM trackers ORDER BY id")
        return cur.fetchall()


def _read_redmine_issues(conn) -> list:
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT i.id, i.subject, i.description, i.created_on, i.updated_on,
                   i.due_date, i.author_id, i.assigned_to_id, i.parent_id,
                   i.tracker_id,
                   i.status_id,
                   s.name AS status_name, s.is_closed,
                   p.name AS priority_name,
                   t.name AS tracker_name
            FROM issues i
            JOIN issue_statuses s ON i.status_id = s.id
            JOIN enumerations p ON i.priority_id = p.id
            JOIN trackers t ON i.tracker_id = t.id
            ORDER BY i.id
        """)
        return cur.fetchall()


def _read_redmine_journals(conn, issue_id: int) -> list:
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """
            SELECT j.id, j.user_id, j.notes, j.created_on
            FROM journals j
            WHERE j.journalized_id = %s
              AND j.journalized_type = 'Issue'
              AND j.notes IS NOT NULL
              AND j.notes != ''
            ORDER BY j.created_on
        """,
            (issue_id,),
        )
        return cur.fetchall()


def _read_redmine_custom_values(conn, issue_id: int) -> list:
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """
            SELECT cf.name, cv.value
            FROM custom_values cv
            JOIN custom_fields cf ON cv.custom_field_id = cf.id
            WHERE cv.customized_id = %s
              AND cv.customized_type = 'Issue'
              AND cv.value IS NOT NULL
              AND cv.value != ''
        """,
            (issue_id,),
        )
        return cur.fetchall()


def _read_redmine_tags_bulk(conn, tags_table: str) -> dict[int, list[str]]:
    """Return {issue_id: [tag_name, ...]} for all issues via the tags plugin tables."""
    import psycopg2.extras

    taggings_table = "taggings" if tags_table == "tags" else "additional_taggings"
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(f"""
            SELECT tg.taggable_id AS issue_id, t.name AS tag_name
            FROM {tags_table} t
            JOIN {taggings_table} tg ON tg.tag_id = t.id
            WHERE tg.taggable_type = 'Issue'
              AND tg.context = 'tags'
            ORDER BY tg.taggable_id, t.name
        """)  # noqa: S608 — table name comes from our own pg_tables detection, not user input
        result: dict[int, list[str]] = {}
        for row in cur.fetchall():
            result.setdefault(row["issue_id"], []).append(row["tag_name"])
        return result


def _find_tags_custom_field_id(conn, cf_name: str) -> int | None:
    """Return the id of the IssueCustomField with the given name (list type), or None if absent."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM custom_fields
            WHERE type = 'IssueCustomField'
              AND field_format = 'list'
              AND lower(name) = lower(%s)
            LIMIT 1
        """,
            (cf_name,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _read_tags_from_custom_field(conn, tags_cf_id: int) -> dict[int, list[str]]:
    """Return {issue_id: [tag_value, ...]} by reading multi-valued custom_values rows."""
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """
            SELECT customized_id AS issue_id, value AS tag_name
            FROM custom_values
            WHERE custom_field_id = %s
              AND customized_type = 'Issue'
              AND value IS NOT NULL
              AND value <> ''
            ORDER BY customized_id, value
        """,
            (tags_cf_id,),
        )
        result: dict[int, list[str]] = {}
        for row in cur.fetchall():
            result.setdefault(row["issue_id"], []).append(row["tag_name"])
        return result


def _resolve_state_id(
    redmine_status_id: int,
    migration_map: dict,
) -> int | None:
    """Return the Zammad state ID for a Redmine issue status."""
    return migration_map["states"].get(f"redmine_status_{redmine_status_id}")


def _state_is_fallback(redmine_status_id: int, migration_map: dict) -> bool:
    """Return True if the state was mapped via the pending-reminder fallback (no name match)."""
    return bool(migration_map["states"].get(f"redmine_status_{redmine_status_id}__fallback"))


# --- Migration steps ---


def _resolve_and_store_states(conn, api: ZammadAPI, migration_map: dict, toml: dict, error_log: logging.Logger) -> None:
    """Resolve Redmine statuses → Zammad state IDs via the TOML [states] mapping.

    For each Redmine status, reads the `zammad_type` from TOML and picks the first
    existing Zammad state whose state_type matches. Does NOT create new Zammad states.

    If the Redmine status has no TOML entry (or the mapped type has no matching Zammad
    state), falls back to "pending reminder" and marks the key with __fallback so
    _migrate_tickets sets pending_time = created_on.

    migration_map["states"]["redmine_status_<id>"] = <zammad_state_id>
    migration_map["states"]["redmine_status_<id>__fallback"] = True  (fallback only)
    """
    state_map = toml.get("states", {})

    with conn.cursor() as cur:
        cur.execute("SELECT id, name, is_closed FROM issue_statuses ORDER BY id")
        redmine_statuses = cur.fetchall()

    existing_states = api.get("ticket_states?expand=true")
    # Build state_type → first matching Zammad state ID.
    zammad_type_to_id: dict[str, int] = {}
    for s in existing_states:
        st = s.get("state_type", "")
        if st and st not in zammad_type_to_id:
            zammad_type_to_id[st] = s["id"]

    pending_reminder_id = zammad_type_to_id.get(_StateType.PENDING_REMINDER.value)

    print(f"\nResolving {len(redmine_statuses)} Redmine statuses → Zammad states...")
    stored = skipped = fallbacks = 0

    for row in redmine_statuses:
        redmine_id, status_name, _is_closed = row[0], row[1], row[2]
        redmine_key = f"redmine_status_{redmine_id}"

        if redmine_key in migration_map["states"]:
            skipped += 1
            continue

        cfg = state_map.get(status_name, {})
        zammad_type = cfg.get(TOML_STATE_TYPE_KEY)
        zammad_state_id = zammad_type_to_id.get(zammad_type) if zammad_type else None

        if zammad_state_id:
            migration_map["states"][redmine_key] = zammad_state_id
            stored += 1
        else:
            if not zammad_type:
                error_log.warning(f"State '{status_name}': not in TOML [states] — falling back to 'pending reminder'")
            else:
                error_log.warning(
                    f"State '{status_name}': no Zammad state with type '{zammad_type}'"
                    " — falling back to 'pending reminder'"
                )
            if not pending_reminder_id:
                error_log.error(f"State '{status_name}': 'pending reminder' state not found in Zammad — skipping")
                continue
            migration_map["states"][redmine_key] = pending_reminder_id
            migration_map["states"][f"{redmine_key}__fallback"] = True
            fallbacks += 1
            stored += 1

    fallback_note = f", {fallbacks} fell back to pending reminder" if fallbacks else ""
    print(f"  States: {stored} resolved, {skipped} skipped (already done){fallback_note}")
    _save_migration_map(migration_map, api.dry_run)


def _ensure_redmine_status_field(conn, api: ZammadAPI, migration_map: dict, error_log: logging.Logger) -> bool:
    """Create (or find) a 'Redmine Status' select custom field populated with all Redmine status names.

    Idempotent: skips creation if already recorded in migration_map.
    Returns True if the field is ready, False on failure.
    """
    if _REDMINE_STATUS_CF_KEY in migration_map.get("custom_fields", {}):
        return True

    with conn.cursor() as cur:
        cur.execute("SELECT name FROM issue_statuses ORDER BY id")
        status_names = [row[0] for row in cur.fetchall()]

    object_data = {
        "name": _REDMINE_STATUS_CF_NAME,
        "display": "Redmine Status",
        "data_type": "select",
        "object": "Ticket",
        "active": True,
        "position": 898,
        "data_option": {
            "options": {n: n for n in status_names},
            "default": "",
            "nulloption": True,
            "null": True,
        },
        "screens": {
            "create_middle": {"ticket.agent": {"shown": True}},
            "edit": {"ticket.agent": {"shown": True}},
        },
    }
    try:
        result = api.post("object_manager_attributes", object_data)
    except ZammadAPIError as post_err:
        all_attrs = api.get("object_manager_attributes")
        match = next(
            (a for a in all_attrs if a.get("object") == "Ticket" and a.get("name") == _REDMINE_STATUS_CF_NAME),
            None,
        )
        if not match:
            error_log.error(f"redmine_status field: creation failed ({post_err}) and field not found")
            return False
        result = match

    migration_map.setdefault("custom_fields", {})[_REDMINE_STATUS_CF_KEY] = {
        "zammad_name": _REDMINE_STATUS_CF_NAME,
        "id": result.get("id", -1),
    }

    if not api.dry_run:
        try:
            api.post("object_manager_attributes_execute_migrations", {})
        except ZammadAPIError as e:
            error_log.error(f"redmine_status field: execute_migrations failed: {e}")

    return True


def _upsert_zammad_user(
    api: ZammadAPI,
    fields: dict,
    roles: list[str],
    error_log: logging.Logger,
    label: str,
) -> dict | None:
    """POST a new Zammad user; on conflict find by email then login, PUT to sync fields.

    Returns the Zammad user dict on success, None on failure.
    `fields` must contain at least 'login', 'email', 'firstname', 'lastname'.
    `label` is used in error messages (e.g. "User 42 (jdoe)" or "Customer john@example.com").
    """
    login = fields["login"]
    email = fields["email"]
    try:
        return api.post("users", {**fields, "roles": roles})
    except ZammadAPIError as post_err:
        match = None
        if email:
            by_email = api.search(f"users/search?query=email:{email}&limit=1")
            match = next((u for u in by_email if u.get("email") == email), None)
        if not match:
            by_login = api.search(f"users/search?query=login:{login}&limit=1")
            match = next((u for u in by_login if u.get("login") == login), None)
        if not match:
            error_log.error(f"{label}: creation failed ({post_err}) and not found by email or login")
            return None
        # Sync fields; don't overwrite login if the existing account uses a different one —
        # that would cause a "Login already taken" conflict with itself or another user.
        update = dict(fields)
        if match.get("login") != login:
            del update["login"]
        try:
            api.put(f"users/{match['id']}", update)
        except ZammadAPIError as e:
            error_log.error(f"{label} update after find: {e}")
        return match


def _migrate_users(conn, api: ZammadAPI, migration_map: dict, error_log: logging.Logger) -> None:
    users = _read_redmine_users(conn)
    print(f"\nMigrating {len(users)} users...")
    migrated = skipped = updated = 0

    def _fields(user, login, email) -> dict:
        return {
            "login": login,
            "firstname": user["firstname"] or "Unknown",
            "lastname": user["lastname"] or "User",
            "email": email,
            "active": user["status"] == 1,
        }

    for user in users:
        redmine_id = str(user["id"])
        login = user["login"] or f"redmine_user_{user['id']}"
        email = user["mail"] or f"redmine_user_{user['id']}@migration.local"
        fields = _fields(user, login, email)
        label = f"User {user['id']} ({login})"

        if redmine_id in migration_map["users"]:
            zammad_id = migration_map["users"][redmine_id]
            try:
                api.put(f"users/{zammad_id}", fields)
                updated += 1
            except ZammadAPIError as e:
                error_log.error(f"{label} update: {e}")
            skipped += 1
            continue

        # Query first — never POST if the user already exists in Zammad.
        existing = None
        if email:
            by_email = api.search(f"users/search?query=email:{email}&limit=1")
            existing = next((u for u in by_email if u.get("email") == email), None)
        if not existing:
            by_login = api.search(f"users/search?query=login:{login}&limit=1")
            existing = next((u for u in by_login if u.get("login") == login), None)

        if existing:
            update = dict(fields)
            if existing.get("login") != login:
                del update["login"]
            try:
                api.put(f"users/{existing['id']}", update)
            except ZammadAPIError as e:
                error_log.error(f"{label} update after find: {e}")
            migration_map["users"][redmine_id] = existing["id"]
            migrated += 1
            continue

        try:
            result = api.post("users", {**fields, "roles": ["Agent", "Customer"]})
        except ZammadAPIError as e:
            error_log.error(f"{label}: creation failed: {e}")
            continue
        migration_map["users"][redmine_id] = result["id"]
        migrated += 1

    update_note = f", {updated} updated" if updated else ""
    print(f"  Users: {migrated} migrated, {skipped} skipped{update_note}")
    _save_migration_map(migration_map, api.dry_run)


def _migrate_organizations(api: ZammadAPI, migration_map: dict, toml: dict, error_log: logging.Logger) -> None:
    """Create Zammad organizations from the [organizations] table in zammad.toml.

    TOML format:
        [organizations]
        "Acme Corp" = { domain = "acme.com", domain_assignment = true, note = "..." }
        "SBNF"      = {}   # name only, no extra fields required

    All fields except the name are optional.
    Idempotent: skips entries already recorded in migration_map["organizations"].
    """
    orgs = toml.get("organizations", {})
    if not orgs:
        return

    print(f"\nMigrating {len(orgs)} organizations...")
    migrated = skipped = 0
    migration_map.setdefault("organizations", {})

    for name, cfg in orgs.items():
        org_key = name
        if org_key in migration_map["organizations"]:
            skipped += 1
            continue

        payload: dict = {"name": name, "active": True}
        if cfg.get("domain"):
            payload["domain"] = cfg["domain"]
        if cfg.get("domain_assignment"):
            payload["domain_assignment"] = bool(cfg["domain_assignment"])
        if cfg.get("note"):
            payload["note"] = cfg["note"]

        try:
            result = api.post("organizations", payload)
        except ZammadAPIError as post_err:
            # Already exists — find by name.
            all_orgs = api.search("organizations")
            match = next((o for o in all_orgs if o.get("name") == name), None)
            if not match:
                error_log.error(f"Organization '{name}': creation failed ({post_err}) and not found by name")
                continue
            result = match

        migration_map["organizations"][org_key] = result["id"]
        migrated += 1

    print(f"  Organizations: {migrated} created, {skipped} skipped")
    _save_migration_map(migration_map, api.dry_run)


def _migrate_customers(api: ZammadAPI, migration_map: dict, toml: dict, error_log: logging.Logger) -> None:
    """Create Zammad customer accounts from the [customers] table in zammad.toml.

    TOML format:
        [customers]
        "john@example.com" = { firstname = "John", lastname = "Doe", login = "john" }
        "jane@example.com" = {}  # firstname/lastname/login derived from email if omitted

    The email address is the unique key. All other fields are optional.
    Idempotent: skips entries already recorded in migration_map["customers"].
    """
    customers = toml.get("customers", {})
    if not customers:
        return

    print(f"\nMigrating {len(customers)} customers from TOML...")
    migrated = skipped = 0
    migration_map.setdefault("customers", {})

    org_name_to_id: dict[str, int] = {}

    for email, cfg in customers.items():
        if email in migration_map["customers"]:
            skipped += 1
            continue

        local = email.split("@")[0]
        firstname = cfg.get("firstname") or local.capitalize()
        lastname = cfg.get("lastname") or ""
        login = cfg.get("login") or local

        payload: dict = {
            "email": email,
            "login": login,
            "firstname": firstname,
            "lastname": lastname,
            "active": True,
        }

        # Resolve optional organization by name.
        org_name = cfg.get("organization")
        if org_name:
            if org_name not in org_name_to_id:
                orgs = api.search("organizations")
                org_name_to_id = {o["name"]: o["id"] for o in orgs if o.get("name")}
            org_id = org_name_to_id.get(org_name)
            if org_id:
                payload["organization_id"] = org_id
            else:
                error_log.warning(f"Customer '{email}': organization '{org_name}' not found — skipping org assignment")

        result = _upsert_zammad_user(api, payload, ["Customer"], error_log, f"Customer '{email}'")
        if result is None:
            continue

        migration_map["customers"][email] = result["id"]
        migrated += 1

    print(f"  Customers: {migrated} created, {skipped} skipped")
    _save_migration_map(migration_map, api.dry_run)


def _migrate_group(conn, api: ZammadAPI, migration_map: dict, toml: dict, error_log: logging.Logger) -> None:
    import_group = toml.get("import_group", "Redmine Import")

    if MIGRATION_MAP_GROUP_KEY not in migration_map["groups"]:
        print(f"\nCreating group '{import_group}'...")
        try:
            result = api.post("groups", {"name": import_group, "active": True})
        except ZammadAPIError as post_err:
            # Group already exists in Zammad (e.g. partial previous run) — fetch its ID by name.
            groups = api.get("groups")
            match = next((g for g in groups if g["name"] == import_group), None)
            if not match:
                msg = f"Group '{import_group}': creation failed ({post_err}) and could not be found by name"
                error_log.error(msg)
                raise ZammadAPIError(msg) from None
            result = match
        migration_map["groups"][MIGRATION_MAP_GROUP_KEY] = result["id"]
        _save_migration_map(migration_map, api.dry_run)

    parent_group_id = migration_map["groups"][MIGRATION_MAP_GROUP_KEY]

    # Create one child group per Redmine tracker (issue type), nested under the import group.
    trackers = _read_redmine_trackers(conn)
    print(f"  Creating {len(trackers)} tracker groups...")
    for tracker in trackers:
        tracker_key = f"tracker_{tracker['id']}"
        if tracker_key in migration_map["groups"]:
            continue
        try:
            result = api.post("groups", {"name": tracker["name"], "parent_id": parent_group_id, "active": True})
        except ZammadAPIError as post_err:
            groups = api.get("groups")
            full_name = f"{import_group}::{tracker['name']}"
            match = next((g for g in groups if g["name"] == full_name), None)
            if not match:
                error_log.error(
                    f"Tracker group '{tracker['name']}': creation failed ({post_err}) and not found by name"
                )
                continue
            result = match
        migration_map["groups"][tracker_key] = result["id"]
    _save_migration_map(migration_map, api.dry_run)

    # Collect all group IDs users must belong to: parent + all tracker child groups.
    all_group_ids = [parent_group_id] + [
        migration_map["groups"][f"tracker_{t['id']}"]
        for t in trackers
        if f"tracker_{t['id']}" in migration_map["groups"]
    ]

    # Ensure every migrated user (including API token user) is a member of all groups.
    # Zammad rejects owner_id or customer_id for users not in the ticket's group.
    user_ids_to_add = list(migration_map["users"].values())
    me = api.get("users/me")
    user_ids_to_add.append(me["id"])
    print(f"  Ensuring {len(user_ids_to_add)} users are members of {len(all_group_ids)} groups...")
    for uid in user_ids_to_add:
        user = api.get(f"users/{uid}")
        current_groups = dict(user.get("group_ids", {}))
        new_entries = {
            gid: ["full"] for gid in all_group_ids if gid not in current_groups and str(gid) not in current_groups
        }
        if new_entries:
            api.put(f"users/{uid}", {"group_ids": new_entries})


def _migrate_custom_fields(
    conn, api: ZammadAPI, migration_map: dict, error_log: logging.Logger, skip_cf_id: int | None = None
) -> None:
    fields = _read_redmine_custom_fields(conn)
    print(f"\nMigrating {len(fields)} custom field definitions...")
    migrated = skipped = 0

    # Zammad data_type mapping from Redmine field_format.
    format_map = {
        "string": "input",
        "text": "textarea",
        "int": "integer",
        "float": "input",
        "date": "date",
        "bool": "boolean",
        "list": "select",
        "link": "input",
    }
    # Required data_option defaults per Zammad data_type (API rejects fields without these).
    default_data_option: dict[str, dict] = {
        "input": {"default": "", "maxlength": 255, "null": True, "type": "text"},
        "textarea": {"default": "", "rows": 4, "null": True},
        "integer": {"default": None, "min": 0, "max": 999999, "null": True},
        "boolean": {"default": False, "null": True},
        "date": {"diff": 0, "null": True},
        "datetime": {"diff": 0, "null": True},
        "select": {"default": "", "nulloption": True, "null": True, "options": {}, "relation": ""},
    }
    for field in fields:
        redmine_id = str(field["id"])
        if skip_cf_id is not None and field["id"] == skip_cf_id:
            print(f"  Skipping '{field['name']}' (id={field['id']}) — migrated as native Zammad tags.")
            skipped += 1
            continue
        if redmine_id in migration_map["custom_fields"]:
            skipped += 1
            continue

        zammad_name = _sanitize_field_name(field["name"])
        data_type = format_map.get(field["field_format"], "input")

        object_data = {
            "name": zammad_name,
            "display": field["name"].title(),
            "data_type": data_type,
            "object": "Ticket",
            "active": True,
            "position": 900 + int(redmine_id),
            "data_option": dict(default_data_option.get(data_type, {})),
            "screens": {
                "create_middle": {"ticket.agent": {"shown": True}},
                "edit": {"ticket.agent": {"shown": True}},
            },
        }

        if data_type == "select" and field["possible_values"]:
            try:
                import yaml

                values = yaml.safe_load(field["possible_values"])
                if isinstance(values, list):
                    object_data["data_option"] = {
                        "options": {v: v for v in values},
                        "default": field["default_value"] or "",
                        "nulloption": True,
                        "null": True,
                    }
            except Exception:
                error_log.debug("Could not parse possible_values for field '%s'", field["name"])

        try:
            result = api.post("object_manager_attributes", object_data)
        except ZammadAPIError as post_err:
            # Field already exists — fetch all attributes and filter by object type + name.
            all_attrs = api.get("object_manager_attributes")
            match = next((a for a in all_attrs if a.get("object") == "Ticket" and a.get("name") == zammad_name), None)
            if not match:
                error_log.error(
                    f"Custom field {field['id']} ({field['name']}): creation failed ({post_err}) and field not found"
                )
                continue
            result = match
        except Exception as e:
            error_log.error(f"Custom field {field['id']} ({field['name']}): {e}")
            continue
        migration_map["custom_fields"][redmine_id] = {"zammad_name": zammad_name, "id": result.get("id", -1)}
        migrated += 1

    if migrated > 0 and not api.dry_run:
        print("  Applying object attribute changes in Zammad...")
        try:
            api.post("object_manager_attributes_execute_migrations", {})
        except Exception as e:
            error_log.error(f"Failed to execute object manager migration: {e}")

    print(f"  Custom fields: {migrated} migrated, {skipped} skipped")
    _save_migration_map(migration_map, api.dry_run)


def _resolve_default_customer(
    conn, api: ZammadAPI, toml: dict, migration_map: dict, error_log: logging.Logger
) -> int | None:
    """Return the Zammad user ID for default_customer_login from TOML, or None if not configured.

    Checks the migration map first (for migrated Redmine users), then falls back to the API
    search (for pre-existing Zammad users like the built-in admin).
    """
    login = toml.get("default_customer_login", "")
    if not login:
        return None

    # Try to find the Redmine user ID for this login, then look it up in the migration map.
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE login = %s AND type = 'User' LIMIT 1", (login,))
        row = cur.fetchone()
    if row:
        zammad_id = migration_map["users"].get(str(row[0]))
        if zammad_id:
            return zammad_id

    # Fall back to searching Zammad directly (e.g. built-in admin not in the migration map).
    # The search index may not include system users, so also try listing all users as a fallback.
    results = api.search(f"users/search?query=login:{login}&limit=1")
    match = next((u for u in results if u.get("login") == login), None)
    if not match:
        all_users = api.get("users?expand=true")
        if isinstance(all_users, list):
            match = next((u for u in all_users if u.get("login") == login), None)
    if not match:
        error_log.error(f"default_customer_login '{login}' not found in Zammad")
        return None
    return match["id"]


def _migrate_tickets(
    conn,
    api: ZammadAPI,
    migration_map: dict,
    toml: dict,
    tags_by_issue: dict[int, list[str]],
    error_log: logging.Logger,
    tags_cf_id: int | None = None,
    tags_cf_name: str = "Tags",
) -> None:
    group_id = migration_map["groups"].get(MIGRATION_MAP_GROUP_KEY)
    if not group_id:
        error_log.error("No group_id in migration_map — group migration must have failed. Aborting ticket migration.")
        return

    default_customer_id = _resolve_default_customer(conn, api, toml, migration_map, error_log)

    from tqdm import tqdm

    issues = _read_redmine_issues(conn)
    print(f"\nMigrating {len(issues)} issues → tickets...")
    migrated = skipped = 0
    priority_map = toml.get("priorities", {})
    redmine_base_url = toml.get("redmine_url", "").rstrip("/")

    try:
        _all_zammad_states = {s["name"]: s["id"] for s in api.get("ticket_states")}
        pending_reminder_state_id: int | None = _all_zammad_states.get(_StateType.PENDING_REMINDER.value)
    except ZammadAPIError:
        pending_reminder_state_id = None

    repaired = 0
    for issue in tqdm(issues, desc="Migrating tickets", unit="ticket"):
        redmine_id = str(issue["id"])

        custom_values = _read_redmine_custom_values(conn, issue["id"])
        custom_fields_data: dict = {}
        for cv in custom_values:
            if tags_cf_id is not None and cv["name"].lower() == tags_cf_name.lower():
                continue  # Already included via tags_by_issue as native Zammad tags
            custom_fields_data[_sanitize_field_name(cv["name"])] = cv["value"]

        if redmine_id in migration_map["tickets"]:
            # Ticket already migrated — repair custom field values in case they were dropped
            # (e.g. if the ticket was created before execute_migrations ran and activated fields).
            zammad_id = migration_map["tickets"][redmine_id]
            repair_data = dict(custom_fields_data)
            repair_data[_REDMINE_STATUS_CF_NAME] = issue["status_name"]
            try:
                api.put(f"tickets/{zammad_id}", repair_data)
                repaired += 1
            except ZammadAPIError as e:
                error_log.error(f"Issue {issue['id']} repair custom fields: {e}")
            # Backfill first-article ID if not yet recorded.
            if f"{redmine_id}_first" not in migration_map.get("articles", {}):
                try:
                    articles = api.search(f"ticket_articles/by_ticket/{zammad_id}")
                    if articles:
                        first_article_id = min(a["id"] for a in articles)
                        migration_map.setdefault("articles", {})[f"{redmine_id}_first"] = first_article_id
                except Exception as e:
                    error_log.error(f"Issue {issue['id']}: could not fetch first article ID: {e}")
            skipped += 1
            continue

        customer_id = migration_map["users"].get(str(issue["author_id"])) or default_customer_id
        if not customer_id:
            error_log.error(
                f"Issue {issue['id']} ({issue['subject'][:50]}): no customer_id "
                "(author not migrated and no default_customer_login configured) — skipping"
            )
            skipped += 1
            continue
        owner_id = migration_map["users"].get(str(issue["assigned_to_id"])) if issue["assigned_to_id"] else None

        state_id = _resolve_state_id(issue["status_id"], migration_map)
        priority = priority_map.get(issue["priority_name"], _Priority.NORMAL)

        created_at = issue["created_on"].isoformat() if issue["created_on"] else None
        updated_at = issue["updated_on"].isoformat() if issue["updated_on"] else None

        tracker_group_id = migration_map["groups"].get(f"tracker_{issue['tracker_id']}", group_id)
        ticket_data = {
            "title": issue["subject"],
            "group_id": tracker_group_id,
            "customer_id": customer_id,
            "owner_id": owner_id,
            "state_id": state_id,
            "priority": priority,
            _REDMINE_STATUS_CF_NAME: issue["status_name"],
            "article": {
                "subject": issue["subject"],
                "body": _issue_body(issue["description"], issue["id"], redmine_base_url),
                "content_type": "text/html",
                "type": "note",
                "internal": False,
            },
        }

        if created_at:
            ticket_data["created_at"] = created_at
            ticket_data["article"]["created_at"] = created_at
        if updated_at:
            ticket_data["updated_at"] = updated_at

        # pending_time is required when the state is "pending reminder".
        # Case 1: issue has a due_date → force pending_reminder, use due_date.
        # Case 2: status mapped to pending_reminder via fallback → use created_on.
        if issue["due_date"] and not issue["is_closed"]:
            if pending_reminder_state_id is None:
                # None means the API call to fetch ticket states failed (see above); the
                # "pending reminder" state exists in every default Zammad installation so
                # its absence indicates a fetch error, not a missing state.
                error_log.error(
                    f"Issue {issue['id']}: has due_date but 'pending reminder' state not found — pending_time lost"
                )
            else:
                if state_id != pending_reminder_state_id:
                    issue_url = (
                        f"{redmine_base_url}/issues/{issue['id']}" if redmine_base_url else f"Issue {issue['id']}"
                    )
                    due = issue["due_date"].strftime("%d/%m/%Y")
                    status = issue["status_name"]
                    error_log.warning(
                        f"{issue_url} status '{status}' → state 'pending reminder' to preserve due date {due}"
                    )
                ticket_data["state_id"] = pending_reminder_state_id
                ticket_data["pending_time"] = issue["due_date"].isoformat()
        elif _state_is_fallback(issue["status_id"], migration_map) and created_at:
            ticket_data["pending_time"] = created_at

        tags = tags_by_issue.get(issue["id"], [])
        if tags:
            ticket_data["tags"] = ",".join(tags)

        ticket_data.update(custom_fields_data)

        try:
            result = api.post("tickets", ticket_data)
            zammad_ticket_id = result["id"]
            migration_map["tickets"][redmine_id] = zammad_ticket_id
            for tag in tags:
                migration_map.setdefault("tags", {})[f"{redmine_id}:{tag}"] = zammad_ticket_id
            # Save the first article ID so the body-repair step can find it later.
            try:
                articles = api.search(f"ticket_articles/by_ticket/{zammad_ticket_id}")
                if articles:
                    first_article_id = min(a["id"] for a in articles)
                    migration_map["articles"][f"{redmine_id}_first"] = first_article_id
            except Exception as e:
                error_log.error(f"Issue {issue['id']}: could not fetch first article ID: {e}")
            migrated += 1
            if migrated % 50 == 0:
                _save_migration_map(migration_map, api.dry_run)
        except Exception as e:
            error_log.error(f"Issue {issue['id']} ({issue['subject'][:50]}): {e}")

    repair_note = f", {repaired} custom-field repairs" if repaired else ""
    print(f"  Tickets: {migrated} migrated, {skipped} skipped{repair_note}")
    _save_migration_map(migration_map, api.dry_run)


def _migrate_articles(conn, api: ZammadAPI, migration_map: dict, error_log: logging.Logger) -> None:
    from tqdm import tqdm

    print("\nMigrating journal entries → articles...")
    migrated = skipped = 0

    for redmine_issue_id, zammad_ticket_id in tqdm(
        migration_map["tickets"].items(), desc="Migrating articles", unit="ticket"
    ):
        journals = _read_redmine_journals(conn, int(redmine_issue_id))

        for journal in journals:
            article_key = f"{redmine_issue_id}_{journal['id']}"
            if article_key in migration_map["articles"]:
                skipped += 1
                continue

            created_at = journal["created_on"].isoformat() if journal["created_on"] else None

            article_data: dict = {
                "ticket_id": zammad_ticket_id,
                "body": _md_to_html(journal["notes"]),
                "content_type": "text/html",
                "type": "note",
                "internal": False,
            }
            if created_at:
                article_data["created_at"] = created_at

            try:
                result = api.post("ticket_articles", article_data)
                migration_map["articles"][article_key] = result["id"]
                migrated += 1
            except Exception as e:
                error_log.error(f"Journal {journal['id']} on issue {redmine_issue_id}: {e}")

    print(f"  Articles: {migrated} migrated, {skipped} skipped")
    _save_migration_map(migration_map, api.dry_run)


def _repair_article_bodies(
    migration_map: dict, redmine_base_url: str, dry_run: bool, skip_first_keys: set[str] | None = None
) -> None:
    r"""Ensure every first-article body starts with the Redmine issue URL header.

    The header format is:
        <p><a href="URL">URL</a></p>\n
    Articles are immutable via Zammad's REST API, so we update the DB directly.
    Only processes tickets whose first-article ID was recorded in migration_map["articles"].
    `skip_first_keys`: set of "<redmine_id>_first" keys to exclude — used to skip articles
    whose body was already written with the URL header by _issue_body on this run.
    """
    import re
    import subprocess

    if not redmine_base_url:
        return

    first_articles = {
        k.removesuffix("_first"): v
        for k, v in migration_map.get("articles", {}).items()
        if k.endswith("_first") and (skip_first_keys is None or k not in skip_first_keys)
    }
    if not first_articles:
        return

    safe_url = redmine_base_url.replace("'", "''")
    # Pattern matches an existing URL header for any issue number — used to skip articles that
    # already have the header (idempotency guard).
    existing_header_pattern = f'<p><a href="{safe_url}/issues/[0-9]+">[^<]*</a></p>'

    # Build one UPDATE per article so we can inject the correct issue-specific URL.
    statements: list[str] = []
    for redmine_id, article_id in first_articles.items():
        url = f"{safe_url}/issues/{redmine_id}"
        header = f'<p><a href="{url}">{url}</a></p>\\n'
        statements.append(
            f"UPDATE ticket_articles "  # noqa: S608 — values come from migration_map.json, not user input
            f"SET body = '{header}' || body "
            f"WHERE id = {int(article_id)} "
            f"AND body !~ '{existing_header_pattern}';"
        )

    if dry_run:
        print(f"  [DRY-RUN] Would restore Redmine URL header in up to {len(statements)} article(s)")
        return

    print(f"\nRestoring Redmine URL header in up to {len(statements)} first article(s)...")
    sql = "\n".join(statements)
    # Pass SQL via stdin to avoid "argument list too long" with hundreds of statements.
    result = subprocess.run(
        ["docker", "exec", "-i", "postgres17", "psql", "-U", "postgres", "-d", "zammad"],
        input=sql,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print_error(f"  ❌ DB update failed: {result.stderr.strip()}")
    else:
        updated = sum(int(m) for m in re.findall(r"UPDATE (\d+)", result.stdout))
        print(f"  Article bodies: {updated} updated")


def _migrate_links(conn, api: ZammadAPI, migration_map: dict, error_log: logging.Logger) -> None:
    """Create parent/child links in Zammad for Redmine issues that have a parent_id."""
    import psycopg2.extras
    from tqdm import tqdm

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT id, parent_id FROM issues
            WHERE parent_id IS NOT NULL
            ORDER BY id
        """)
        parent_rows = cur.fetchall()

    if not parent_rows:
        return

    # Only process pairs where both sides were successfully migrated.
    pairs = [
        (row["id"], row["parent_id"])
        for row in parent_rows
        if str(row["id"]) in migration_map["tickets"] and str(row["parent_id"]) in migration_map["tickets"]
    ]
    if not pairs:
        print("\nNo parent/child links to migrate (no matching ticket pairs in map).")
        return

    print(f"\nMigrating {len(pairs)} parent/child links...")

    # Bulk-fetch Zammad ticket numbers for all involved ticket IDs (one request per 100 IDs).
    # The links/add API requires the source ticket's display number, not its internal ID.
    zammad_ids_needed = {migration_map["tickets"][str(child)] for child, _ in pairs} | {
        migration_map["tickets"][str(parent)] for _, parent in pairs
    }
    id_to_number: dict[int, str] = {}
    for zammad_id in zammad_ids_needed:
        try:
            t = api.get(f"tickets/{zammad_id}")
            id_to_number[zammad_id] = t["number"]
        except Exception as e:
            error_log.error(f"Could not fetch ticket number for Zammad ticket {zammad_id}: {e}")

    migrated = skipped = 0
    for redmine_child_id, redmine_parent_id in tqdm(pairs, desc="Migrating links", unit="link"):
        link_key = f"{redmine_child_id}_{redmine_parent_id}"
        if link_key in migration_map.get("links", {}):
            skipped += 1
            continue

        zammad_parent_id = migration_map["tickets"][str(redmine_parent_id)]
        zammad_child_id = migration_map["tickets"][str(redmine_child_id)]
        parent_number = id_to_number.get(zammad_parent_id)
        if not parent_number:
            error_log.error(f"Issue {redmine_child_id}: parent ticket number not available, skipping link.")
            continue

        try:
            api.post(
                "links/add",
                {
                    "link_type": "parent",
                    "link_object_source": "Ticket",
                    "link_object_source_number": parent_number,
                    "link_object_target": "Ticket",
                    "link_object_target_value": zammad_child_id,
                },
            )
            migration_map.setdefault("links", {})[link_key] = True
            migrated += 1
            if migrated % 50 == 0:
                _save_migration_map(migration_map, api.dry_run)
        except Exception as e:
            error_log.error(f"Issue {redmine_child_id} → parent {redmine_parent_id}: {e}")

    print(f"  Links: {migrated} migrated, {skipped} skipped")
    _save_migration_map(migration_map, api.dry_run)


def _read_redmine_queries(conn) -> list:
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT id, name, filters, sort_criteria, group_by, user_id, visibility
            FROM queries
            WHERE type = 'IssueQuery'
            ORDER BY id
        """)
        return cur.fetchall()


def _parse_redmine_filters(
    filters_yaml: str | None,
    migration_map: dict,
    error_log: logging.Logger,
    query_name: str,
    zammad_states_by_type: dict[str, list[str]] | None = None,
) -> dict:
    """Convert Redmine filter YAML into a Zammad overview condition dict.

    Supported field mappings:
      status_id       → ticket.state_id   (operators: =, !, o)
      assigned_to_id  → ticket.owner_id   (operator: =)
      tracker_id      → ticket.group_id   (operator: =, mapped via tracker_N group keys)
      due_date        → ticket.pending_time (operators: !*, =)
      cf_N            → ticket.<zammad_name> (operator: =, !)

    Unsupported fields (child_id, project_id, etc.) are silently skipped.
    """
    import yaml

    if not filters_yaml or filters_yaml.strip() in ("---", "--- {}"):
        return {}

    try:
        raw: dict = yaml.safe_load(filters_yaml) or {}
    except Exception as e:
        error_log.error(f"Overview '{query_name}': could not parse filters YAML: {e}")
        return {}

    # Ruby YAML serialises symbol keys with a leading ":" — strip it for all keys/sub-keys.
    def _strip(d: dict) -> dict:
        return {k.lstrip(":"): v for k, v in d.items()}

    # zammad_states_by_type: state_type_name → [state_id, ...] — passed in from caller.
    # Used to resolve the "o" (open) and "c" (closed) operators.

    # tracker_N group key → zammad_group_id
    tracker_group_ids: dict[str, int] = {
        k: v for k, v in migration_map.get("groups", {}).items() if k.startswith("tracker_")
    }

    # Redmine cf_N → zammad_name (from migration_map["custom_fields"])
    cf_zammad_name: dict[str, str] = {
        f"cf_{rid}": info["zammad_name"]
        for rid, info in migration_map.get("custom_fields", {}).items()
        if isinstance(info, dict) and "zammad_name" in info
    }

    # Operator mapping: Redmine → Zammad
    op_map = {
        "=": "is",
        "!": "is not",
        "~": "contains",
        "!~": "contains not",
        "*": None,  # "any" — omit the condition
        "!*": "is not",  # "none" — is not + empty value list means "not set"
    }

    condition: dict = {}

    for field, raw_filter in raw.items():
        if not isinstance(raw_filter, dict):
            continue
        f = _strip(raw_filter)
        operator_raw: str = str(f.get("operator", "="))
        values: list = f.get("values") or []
        if not isinstance(values, list):
            values = [values]
        values = [str(v) for v in values if v is not None and str(v) != ""]

        # --- status_id ---
        if field == "status_id":
            if operator_raw == "o":
                # Open issues: all Zammad states whose type is not "closed".
                open_ids = [
                    sid
                    for stype, sids in (zammad_states_by_type or {}).items()
                    if stype != _StateType.CLOSED.value
                    for sid in sids
                ]
                if open_ids:
                    condition["ticket.state_id"] = {"operator": "is", "value": open_ids}
            elif operator_raw == "c":
                closed_ids = (zammad_states_by_type or {}).get(_StateType.CLOSED.value, [])
                if closed_ids:
                    condition["ticket.state_id"] = {"operator": "is", "value": closed_ids}
            elif operator_raw in ("=", "!") and values:
                zammad_op = op_map[operator_raw]
                # Resolve Redmine status IDs → Zammad state IDs via the redmine_status_<id>
                # index built by _resolve_and_store_states.
                zammad_ids = [
                    str(migration_map["states"][f"redmine_status_{rid}"])
                    for rid in values
                    if f"redmine_status_{rid}" in migration_map["states"]
                ]
                unresolved = [rid for rid in values if f"redmine_status_{rid}" not in migration_map["states"]]
                if unresolved:
                    error_log.warning(f"Overview '{query_name}': status IDs {unresolved} not in map — skipped.")
                if zammad_ids:
                    condition["ticket.state_id"] = {"operator": zammad_op, "value": zammad_ids}
            continue

        # --- assigned_to_id ---
        if field == "assigned_to_id":
            if operator_raw in ("=", "!") and values:
                zammad_ids = [str(migration_map["users"].get(rid)) for rid in values if migration_map["users"].get(rid)]
                if zammad_ids:
                    condition["ticket.owner_id"] = {"operator": op_map[operator_raw], "value": zammad_ids}
            continue

        # --- tracker_id → group ---
        if field == "tracker_id":
            if operator_raw in ("=", "!") and values:
                gids = [
                    str(tracker_group_ids[f"tracker_{rid}"]) for rid in values if f"tracker_{rid}" in tracker_group_ids
                ]
                if gids:
                    condition["ticket.group_id"] = {"operator": op_map[operator_raw], "value": gids}
            continue

        # --- due_date ---
        # Redmine's "due_date !*" (no due date set) has no direct equivalent in Zammad
        # overview conditions — skip silently rather than emit an invalid condition.
        if field == "due_date":
            continue

        # --- cf_N (custom fields) ---
        if field.startswith("cf_"):
            zammad_name = cf_zammad_name.get(field)
            if not zammad_name:
                continue
            zammad_op = op_map.get(operator_raw)
            if zammad_op is None:
                continue
            if values:
                condition[f"ticket.{zammad_name}"] = {"operator": zammad_op, "value": values}
            continue

        # All other fields (child_id, project_id, etc.) are silently skipped.

    return condition


def _migrate_overviews(conn, api: ZammadAPI, migration_map: dict, _toml: dict, error_log: logging.Logger) -> None:
    queries = sorted(_read_redmine_queries(conn), key=lambda q: q["name"].lower())
    print(f"\nMigrating {len(queries)} Redmine queries → Zammad overviews...")
    migrated = skipped = 0

    # Sort field mapping: Redmine field → Zammad order.by value (bare name, no ticket. prefix).
    sort_field_map = {
        "due_date": "pending_time",
        "updated_on": "updated_at",
        "created_on": "created_at",
        "priority": "priority_id",
        "status": "state_id",
        "id": "number",
        "subject": "title",
        "assigned_to": "owner_id",
    }
    # group_by mapping: Redmine field → Zammad bare field name (no ticket. prefix).
    group_by_map = {
        "status": "state_id",
        "priority": "priority_id",
        "assigned_to": "owner_id",
        "tracker": "group_id",
    }
    # Standard column set shown in overview tables (desktop / small / mobile views).
    default_view = {
        "d": ["title", "customer", "group", "created_at"],
        "s": ["title", "customer", "group", "created_at"],
        "m": ["number", "title", "customer", "group", "created_at"],
        "view_mode_default": "s",
    }
    # Role IDs: 1=Admin, 2=Agent (Zammad built-in, stable across instances).
    agent_role_ids = [1, 2]

    # Fetch all Zammad states once: used for catch-all condition and open/closed operator handling.
    all_zammad_states = api.get("ticket_states?expand=true")
    all_state_ids = [str(s["id"]) for s in all_zammad_states]
    # Build state_type → [str(state_id), ...] for _parse_redmine_filters "o"/"c" operators.
    zammad_states_by_type: dict[str, list[str]] = {}
    for s in all_zammad_states:
        stype = s.get("state_type", "")
        if stype:
            zammad_states_by_type.setdefault(stype, []).append(str(s["id"]))

    migration_map.setdefault("overviews", {})

    for query in queries:
        redmine_id = str(query["id"])
        if redmine_id in migration_map["overviews"]:
            skipped += 1
            continue

        condition = _parse_redmine_filters(
            query["filters"], migration_map, error_log, query["name"], zammad_states_by_type
        )

        # Build sort order from first sort_criteria entry.
        import yaml as _yaml

        order: dict = {"by": "created_at", "direction": "ASC"}
        sort_raw = query["sort_criteria"]
        if sort_raw and sort_raw.strip() not in ("---", ""):
            try:
                sort_list = _yaml.safe_load(sort_raw) or []
                if sort_list and isinstance(sort_list[0], list) and len(sort_list[0]) == 2:  # noqa: PLR2004
                    field, direction = sort_list[0]
                    zammad_field = sort_field_map.get(str(field))
                    if zammad_field:
                        order = {"by": zammad_field, "direction": str(direction).upper()}
            except Exception:  # noqa: S110
                pass

        # group_by (bare field name, no ticket. prefix)
        group_by: str | None = None
        if query["group_by"]:
            group_by = group_by_map.get(str(query["group_by"]))

        overview_data: dict = {
            "name": query["name"],
            "link": _overview_link(query["name"]),
            "condition": condition or {"ticket.state_id": {"operator": "is", "value": all_state_ids}},
            "order": order,
            "view": default_view,
            "active": True,
            "role_ids": agent_role_ids,
        }
        if group_by:
            overview_data["group_by"] = group_by

        try:
            result = api.post("overviews", overview_data)
        except ZammadAPIError as post_err:
            # 422 means invalid conditions — retry with a catch-all and warn.
            # Any other error: check if it already exists (duplicate name on a previous run).
            if post_err.status == HTTPStatus.UNPROCESSABLE_ENTITY:
                error_log.warning(
                    f"Overview '{query['name']}': conditions rejected ({post_err}); retrying with catch-all condition"
                )
                overview_data["condition"] = {"ticket.state_id": {"operator": "is", "value": all_state_ids}}
                try:
                    result = api.post("overviews", overview_data)
                except ZammadAPIError as retry_err:
                    all_overviews = api.search("overviews")
                    match = next((o for o in all_overviews if o.get("name") == query["name"]), None)
                    if not match:
                        error_log.error(
                            f"Overview '{query['name']}': creation failed ({retry_err}) and not found by name"
                        )
                        continue
                    result = match
            else:
                all_overviews = api.search("overviews")
                match = next((o for o in all_overviews if o.get("name") == query["name"]), None)
                if not match:
                    error_log.error(f"Overview '{query['name']}': creation failed ({post_err}) and not found by name")
                    continue
                result = match

        migration_map["overviews"][redmine_id] = result["id"]
        migrated += 1

    print(f"  Overviews: {migrated} migrated, {skipped} skipped")
    _save_migration_map(migration_map, api.dry_run)


def _run_migration(conn, api: ZammadAPI, migration_map: dict, toml: dict, error_log: logging.Logger) -> None:
    """Run all migration steps in order."""
    # Tags can come from a plugin table or from a list-type custom field (name configured in TOML).
    tags_cf_name: str = toml.get("tags_custom_field", "")
    tags_table = _tags_table_exists(conn)
    tags_cf_id = _find_tags_custom_field_id(conn, tags_cf_name) if tags_cf_name else None
    if tags_table:
        print(f"\nTags table detected: '{tags_table}' — tags will be imported.")
        tags_by_issue = _read_redmine_tags_bulk(conn, tags_table)
    elif tags_cf_id is not None:
        print(f"\nTags custom field detected (id={tags_cf_id}) — tags will be imported as native Zammad tags.")
        tags_by_issue = _read_tags_from_custom_field(conn, tags_cf_id)
    else:
        print("\nNo tags source found — skipping tag import.")
        tags_by_issue = {}

    redmine_base_url = toml.get("redmine_url", "").rstrip("/")

    _resolve_and_store_states(conn, api, migration_map, toml, error_log)
    _migrate_custom_fields(conn, api, migration_map, error_log, skip_cf_id=tags_cf_id)
    _ensure_redmine_status_field(conn, api, migration_map, error_log)
    _migrate_organizations(api, migration_map, toml, error_log)
    _migrate_customers(api, migration_map, toml, error_log)
    _migrate_users(conn, api, migration_map, error_log)
    _migrate_group(conn, api, migration_map, toml, error_log)
    _migrate_overviews(conn, api, migration_map, toml, error_log)
    # Snapshot _first keys that exist before this run: only those may need the URL header
    # repaired (tickets from a previous run where redmine_url wasn't set, or the _first key
    # was not yet recorded). Tickets created on this run already have the URL from _issue_body.
    first_keys_before = {k for k in migration_map.get("articles", {}) if k.endswith("_first")}
    _migrate_tickets(
        conn, api, migration_map, toml, tags_by_issue, error_log, tags_cf_id=tags_cf_id, tags_cf_name=tags_cf_name
    )
    # After _migrate_tickets, new _first keys were added (both freshly created tickets and
    # backfilled ones from the skipped path). All of those bodies were written by _issue_body
    # with the URL already present — exclude them from repair.
    first_keys_added_this_run = {
        k for k in migration_map.get("articles", {}) if k.endswith("_first")
    } - first_keys_before
    _repair_article_bodies(migration_map, redmine_base_url, api.dry_run, skip_first_keys=first_keys_added_this_run)
    _migrate_articles(conn, api, migration_map, error_log)
    _migrate_links(conn, api, migration_map, error_log)


# --- Migration task ---


@task(
    help={
        "redmine_db_host": "Redmine PostgreSQL host",
        "redmine_db_port": "Redmine PostgreSQL port",
        "redmine_db_name": "Redmine database name",
        "redmine_db_user": "Redmine database user",
        "redmine_db_pass": "Redmine database password (or POSTGRES_PASSWORD env var)",
        "zammad_url": "Zammad base URL (or ZAMMAD_URL env var)",
        "zammad_token": "Zammad admin API token (or ZAMMAD_TOKEN env var)",
    }
)
def zammad_migrate(
    c: Context,
    redmine_db_host: str = "localhost",
    redmine_db_port: int = 5433,
    redmine_db_name: str = "redmine_migration",
    redmine_db_user: str = "postgres",
    redmine_db_pass: str = "",
    zammad_url: str = "",
    zammad_token: str = "",
) -> None:
    """Migrate Redmine issues to Zammad tickets via REST API.

    Import mode is enabled/disabled automatically. After migration, run:
        invoke zammad-reindex
    """
    dry_run: bool = c.config.run.dry

    if not redmine_db_pass:
        redmine_db_pass = os.environ.get("POSTGRES_PASSWORD", "")
    if not zammad_url:
        zammad_url = os.environ.get("ZAMMAD_URL", "http://localhost:8008")
    if not zammad_token:
        zammad_token = os.environ.get("ZAMMAD_TOKEN", "")

    if not redmine_db_pass:
        print_error("Redmine DB password required: --redmine-db-pass or POSTGRES_PASSWORD env var")
        raise Exit(code=1)
    if not zammad_token:
        print_error("Zammad token required: --zammad-token or ZAMMAD_TOKEN env var")
        raise Exit(code=1)

    try:
        toml = _load_toml()
    except FileNotFoundError as e:
        print_error(str(e))
        raise Exit(code=1) from e

    print("=" * 60)
    print("Redmine → Zammad Migration")
    print("=" * 60)
    if dry_run:
        print_warning("\n⚠️  DRY-RUN MODE — no changes will be made\n")

    error_log, error_counter = _setup_error_logging()
    migration_map = _load_migration_map()
    api = ZammadAPI(zammad_url, zammad_token, dry_run=dry_run)

    print(f"Connecting to Redmine DB at {redmine_db_host}:{redmine_db_port}/{redmine_db_name}...")
    conn = _connect_redmine_db(redmine_db_host, redmine_db_port, redmine_db_name, redmine_db_user, redmine_db_pass)

    if not dry_run:
        _import_mode_on(c)

    try:
        _run_migration(conn, api, migration_map, toml, error_log)
    finally:
        conn.close()
        if not dry_run:
            _import_mode_off(c)

    error_count = error_counter.count
    print("\n" + "=" * 60)
    print("Migration complete!")
    print(f"  Map file:  {MAP_FILE}")
    print(f"  Error log: {ERROR_LOG}")
    print(f"\n  States:        {len(migration_map['states'])}")
    print(f"  Organizations: {len(migration_map.get('organizations', {}))}")
    print(f"  Users:         {len(migration_map['users'])}")
    print(f"  Groups:        {len(migration_map['groups'])}")
    print(f"  Tickets:       {len(migration_map['tickets'])}")
    print(f"  Articles:      {len(migration_map['articles'])}")
    print(f"  Links:         {len(migration_map.get('links', {}))}")
    print(f"  Custom fields: {len(migration_map['custom_fields'])}")
    print(f"  Overviews:     {len(migration_map.get('overviews', {}))}")
    print(f"  Tags:          {len(migration_map.get('tags', {}))}")
    if error_count:
        print_error(f"  Errors:        {error_count} (see {ERROR_LOG})")
    else:
        print("  Errors:        0 ✅")
    print("=" * 60)
