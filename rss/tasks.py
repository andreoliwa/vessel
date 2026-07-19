"""Invoke tasks for the TT-RSS + RSSHub stack."""

import os
import platform
from pathlib import Path

from conjuring.grimoire import lazy_env_variable, print_error, print_normal
from invoke import Context, Exit, task

_VESSEL_DIR = os.environ.get("VESSEL_DIR", "~/dev/me/vessel")
_COMPOSE_BASE = f"-f {Path(_VESSEL_DIR).expanduser()}/rss/compose.yaml"
_COMPOSE_DEV = f"{_COMPOSE_BASE} -f {Path(_VESSEL_DIR).expanduser()}/rss/compose.override.dev.yaml"


def _compose_flags(dev: bool) -> str:
    return _COMPOSE_DEV if dev else _COMPOSE_BASE


def _dry(c: Context) -> bool:
    return bool(c.config.run.dry)


def _detect_dev_mode(c: Context) -> bool:
    """Auto-detect if RSS stack is running in dev mode.

    Detection strategy (in order of reliability):
    1. Check SKIP_RSYNC_ON_STARTUP environment variable (dev-specific)
    2. Check for bind mount vs named volume
    3. Default to normal mode if container not running
    """
    check = c.run("docker ps -a --filter name=ttrss-app --format '{{.Names}}'", hide=True, warn=True)

    if not check or "ttrss-app" not in check.stdout:
        print_normal("RSS stack containers not found, assuming normal mode")
        return False

    env_check = c.run(
        "docker inspect ttrss-app --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -q SKIP_RSYNC_ON_STARTUP",
        hide=True,
        warn=True,
    )

    if env_check and env_check.ok:
        return True

    mount_check = c.run(
        "docker inspect ttrss-app --format"
        " '{{range .Mounts}}{{if eq .Destination \"/var/www/html/tt-rss\"}}{{.Type}}{{end}}{{end}}'",
        hide=True,
        warn=True,
    )

    return bool(mount_check and "bind" in mount_check.stdout)


@task
def rss_setup(c: Context, database: bool = False, plugin: bool = False) -> None:
    """Set up TT-RSS: create database/directories (--database) or install plugin (--plugin)."""
    if not database and not plugin:
        print_error("At least one flag is required: --database or --plugin")
        print("\nUsage:")
        print("  vessel rss setup --database         # Set up database and directories")
        print("  vessel rss setup --plugin           # Install vf_scored plugin")
        print("  vessel rss setup --database --plugin # Do both")
        raise Exit(code=1)

    if database:
        _setup_database(c)

    if plugin:
        _install_plugin(c)


def _setup_database(c: Context) -> None:
    db_name = os.environ.get("TTRSS_DB_NAME", "ttrss")
    db_user = os.environ.get("TTRSS_DB_USER", "ttrss")
    db_pass = lazy_env_variable("TTRSS_DB_PASS", "TT-RSS database password")
    dry = _dry(c)
    data_dir_path = Path(lazy_env_variable("VESSEL_DATA_DIR", "Container apps data directory")).expanduser()

    print_normal("Step 1: Starting PostgreSQL 17...", dry=dry)
    c.run("cd postgres && docker compose up -d postgres17")

    print_normal("\nStep 2: Creating TT-RSS database and user...", dry=dry)
    c.run(f'docker exec postgres17 psql -U postgres -c "CREATE DATABASE {db_name};"')
    c.run(f"docker exec postgres17 psql -U postgres -c \"CREATE USER {db_user} WITH PASSWORD '{db_pass}';\"")
    c.run(f'docker exec postgres17 psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE {db_name} TO {db_user};"')
    c.run(f'docker exec postgres17 psql -U postgres -d {db_name} -c "GRANT ALL ON SCHEMA public TO {db_user};"')

    print_normal("\nStep 3: Creating data directories...", dry=dry)
    ttrss_dirs = data_dir_path / "ttrss"
    for subdir in ["data", "config", "redis"]:
        dir_path = ttrss_dirs / subdir
        dir_path.mkdir(parents=True, exist_ok=True)
        print_normal(f"  Created: {dir_path}", dry=dry)

    print_normal("\nTT-RSS database setup complete!", dry=dry)
    print_normal("\nNext steps:", dry=dry)
    print_normal("  1. vessel rss up", dry=dry)
    print_normal("  2. Open http://localhost:8002/tt-rss", dry=dry)
    print_normal("  3. Login with admin credentials and enable plugins in Preferences.", dry=dry)


def _install_plugin(c: Context) -> None:
    container_name = "ttrss-app"
    plugin_url = "https://github.com/andreoliwa/tt-rss-plugin-vf-scored.git"
    plugin_dir = "/var/www/html/tt-rss/plugins.local"
    plugin_name = "vf_scored"

    dry = _dry(c)

    print_normal(f"Step 1: Checking if {container_name} container is running...", dry=dry)
    result = c.run(f"docker ps --filter name={container_name} --format '{{{{.Names}}}}'", hide=True, warn=True)

    if not result or container_name not in result.stdout:
        print_error(f"Container '{container_name}' is not running!")
        print("\nPlease start the RSS stack first:")
        print("  vessel rss up")
        raise Exit(code=1)

    print_normal(f"Container {container_name} is running", dry=dry)
    print_normal("\nStep 2: Checking if plugin is already installed...", dry=dry)
    check_result = c.run(
        f"docker exec {container_name} test -d {plugin_dir}/{plugin_name} && echo 'exists' || echo 'not found'",
        hide=True,
        warn=True,
    )

    if "exists" in check_result.stdout:
        print_normal(f"Plugin already installed at {plugin_dir}/{plugin_name}", dry=dry)
        print("\nTo reinstall, remove it first:")
        print(f"  docker exec {container_name} rm -rf {plugin_dir}/{plugin_name}")
        return

    print_normal(f"\nStep 3: Installing plugin from {plugin_url}...", dry=dry)
    c.run(f"docker exec {container_name} git clone {plugin_url} {plugin_dir}/{plugin_name}")

    print_normal("\nPlugin installation complete!", dry=dry)
    print_normal("\nNext steps:", dry=dry)
    print_normal("  1. Open http://localhost:8002/tt-rss", dry=dry)
    print_normal("  2. Go to Preferences → Plugins", dry=dry)
    print_normal(f"  3. Enable '{plugin_name}' plugin", dry=dry)
    print_normal("  4. Configure your keyword scoring rules", dry=dry)


@task(
    help={
        "pull": "Update stack before starting (pull images in normal mode, or sync fork + build in dev mode)",
        "dev": "Use dev mode (local tt-rss clone with vf_scored plugin)",
    }
)
def rss_up(c: Context, pull: bool = False, dev: bool = False) -> None:
    """Start the TT-RSS stack."""
    dry = _dry(c)
    cf = _compose_flags(dev)

    if pull:
        if dev:
            ttrss_repo_dir = lazy_env_variable("TTRSS_REPO_DIR", "TT-RSS local repository directory")

            print_normal("Dev mode: Syncing fork, pulling, and building...", dry=dry)
            if platform.system() != "Darwin":
                print_normal("Changing owner and permissions on Linux...", dry=dry)
                c.run(f"chown -R root:root {ttrss_repo_dir}")
                c.run(f"chmod 777 {ttrss_repo_dir}")
            c.run(f"pushd {ttrss_repo_dir} && invoke fork.sync && popd")
            c.run(f"docker compose {_compose_flags(dev=True)} down")
            c.run(f"docker compose {_compose_flags(dev=True)} pull")
            c.run(f"docker compose {_compose_flags(dev=True)} build")
        else:
            print_normal("Normal mode: Pulling latest images...", dry=dry)
            c.run(f"docker compose {_compose_flags(dev=False)} down")
            c.run(f"docker compose {_compose_flags(dev=False)} pull")

    mode_name = "dev" if dev else "normal"
    print_normal(f"Starting TT-RSS stack in {mode_name} mode...", dry=dry)
    c.run(f"docker compose {cf} up -d")
    c.run(f"docker compose {cf} logs -f", warn=True, pty=True)


@task
def rss_down(c: Context) -> None:
    """Stop the TT-RSS stack (auto-detects dev/normal mode)."""
    dev = _detect_dev_mode(c)
    mode_name = "dev" if dev else "normal"
    print_normal(f"Detected {mode_name} mode, stopping TT-RSS stack...", dry=_dry(c))
    c.run(f"docker compose {_compose_flags(dev)} down")
