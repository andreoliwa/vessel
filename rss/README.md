# Personal News Intelligence Stack

A lightweight, hackable, **self-hosted news dashboard** that consolidates RSS, Twitter/X, Telegram, Substack, YouTube,
Reddit, and more — and lets me rank by **my keywords**, not social popularity.

- Officially maintained
  repo: [tt-rss/tt-rss: A free, flexible, open-source, web-based news feed (RSS/Atom/other) reader and aggregator.](https://github.com/tt-rss/tt-rss)

## Requirements & Goals

- **Self-hosted**.
- **Open source** components.
- **Lightweight** (fits 2 vCPU / 4 GB RAM).
- **Mobile-friendly** (Android + iOS via clients).
- **Keyword-first intelligence**:
    - Filter and **score** articles by keywords/regex.
    - Maintain keyword sets via UI (no config editing).
    - Optional Bayesian learning (secondary to explicit rules).
- Ingest from **non-RSS platforms** (Twitter/X, Telegram, Substack, YouTube, Reddit, Instagram…).

## Chosen Architecture

- **Tiny Tiny RSS (TTRSS)** — Core reader with **filters, scoring, labels**, plugins, and a responsive UI.
    - Uses official maintained images from [tt-rss/tt-rss](https://github.com/tt-rss/tt-rss) (GitHub Container Registry)
- **[RSSHub](https://github.com/DIYgod/RSSHub) + Redis** — Feed generator for non-RSS sources; enormous route catalog,
  good caching.
    - `rsshub-internal`: The actual RSSHub service (port 1200)
    - `rsshub`: Nginx proxy on port 80 (works around TT-RSS port stripping bug)
    - Browse routes: [RSSHub Documentation](https://docs.rsshub.app/)
    - Browser extension for easy feed discovery: [RSSHub Radar](https://github.com/DIYgod/RSSHub-Radar)
- **Postgres** — Backend for TTRSS.
- **Docker Compose** — One-file bring-up on macOS.

### Why this combo?

- **Keyword and regex scoring** are first-class in TTRSS.
- **UI-driven filter management** lets me evolve rules easily.
- **RSSHub** covers substantially more sources and options than RSS-Bridge, and its URLs map 1:1 across environments,
  making migration trivial.

## Keyword-First Workflow

- **Filters → Create** rules like:
    - _Content matches_ `(AI|Llama|frontier model|EU AI Act)` → **score +15**, **label: AI/Policy**
    - _Title matches_ `(?i)\b(NBA|transfer|matchday)\b` → **score −20**, optionally **mark as read**
    - _Feed title is_ `"Trusted Analyst"` → **score +50**
- Sort by **Score (desc)** to surface what matters.
- Use **Labels** to both tag and audit which rules triggered.
- Optional **Bayesian plugin**: mark a sample of good/bad items to add adaptive scoring (secondary to explicit rules).

## Setup Instructions

### Prerequisites

1. **PostgreSQL 17** must be running (see `../postgres/compose.yml`)

2. **Python dependencies** for using invoke commands:
    - Install [pipx](https://github.com/pypa/pipx) to manage Python dependencies
    - Install [pyinvoke/invoke](https://github.com/pyinvoke/invoke) to use the `invoke` commands:
        ```bash
        pipx install invoke
        ```
    - Follow the quick setup of [andreoliwa/conjuring](https://github.com/andreoliwa/conjuring#quick-setup) to use
      `invoke fork.sync` in dev mode

3. **Environment variables** must be set (add to your shell profile):

    ```bash
    # Required for all modes
    export VESSEL_DATA_DIR=~/data/
    export TTRSS_DB_NAME=ttrss
    export TTRSS_DB_USER=ttrss
    export TTRSS_DB_PASS=<your-secure-password>
    export TTRSS_ADMIN_PASS=<admin-password>
    export POSTGRES_PASSWORD=<postgres-superuser-password>

    # Optional: override TT-RSS URL (defaults to http://localhost:8002/tt-rss)
    export TTRSS_SELF_URL_PATH=https://news.yourdomain.tld/tt-rss
    ```

    **Important**: Make sure these are exported in your current shell before running docker compose!

4. **For Dev Mode only**: Clone TT-RSS repository locally and set the environment variable:

    ```bash
    git clone https://git.tt-rss.org/fox/tt-rss.git ~/dev/tt-rss
    export TTRSS_REPO_DIR=~/dev/tt-rss

    # Clone the vf_scored plugin into the local TT-RSS clone
    cd ${TTRSS_REPO_DIR}/plugins.local/
    git clone https://github.com/andreoliwa/tt-rss-plugin-vf-scored.git vf_scored
    ```

    **Note**: The vf_scored plugin should be installed in `${TTRSS_REPO_DIR}/plugins.local/vf_scored` (the plugin code).

### First-Time Setup

#### Automated Setup (Recommended)

1. **Run the database setup**:

    ```bash
    vessel rss setup --database
    ```

    This will:
    1. Start PostgreSQL 17
    2. Create the TT-RSS database and user
    3. Create data directories

2. **Start TT-RSS stack** (choose your mode):

    ```bash
    vessel rss up         # normal mode (production)
    vessel rss up --dev   # OR dev mode (local development)
    ```

3. **Access TT-RSS** on http://localhost:8002/tt-rss

4. **Access RSSHub**:
    1. Direct access (for browsing routes): http://localhost:8006/
    2. In TT-RSS feeds, use: `http://rsshub/...` (no port needed, proxy handles it)
    3. Browse available routes at [RSSHub Documentation](https://docs.rsshub.app/)
    4. Install [RSSHub Radar](https://github.com/DIYgod/RSSHub-Radar) browser extension to easily discover and subscribe
       to feeds

## Usage Modes

This setup supports two modes of operation:

1. **Normal Mode (Production)**: Uses official Docker images from GitHub Container Registry. Ideal for production use
   and distribution.
2. **Dev Mode (Local Development)**: Builds from a local TT-RSS clone, allowing source code modifications and plugin
   development.

### Normal Mode (Production)

**What it is:**

- Uses official pre-built Docker images: `ghcr.io/tt-rss/tt-rss:latest`
- Lightweight and easy to distribute
- Perfect for production use or sharing with friends
- Plugins can be installed via TT-RSS's built-in plugin installer

**Starting the stack:**

```bash
vessel rss up
```

**Stopping the stack:**

```bash
vessel rss down
```

**Updating the stack:**

```bash
vessel rss up --pull   # stops, pulls latest images, starts
```

**Installing the vf_scored plugin (Normal Mode):**

The `vf_scored` plugin can be installed using:

```bash
vessel rss setup --plugin
```

This will clone the plugin from GitHub into the running container at `/var/www/html/tt-rss/plugins.local/vf_scored`.

Alternatively, you can use TT-RSS's built-in plugin installer through the web UI (Preferences → Plugins tab).

### Dev Mode (Local Development)

**What it is:**

- Builds Docker images from your local TT-RSS clone (`${TTRSS_REPO_DIR}`)
- Mounts the local repository as a volume, allowing live code changes
- The `vf_scored` plugin is automatically available from `${TTRSS_REPO_DIR}/plugins.local/vf_scored`
- Ideal for developing TT-RSS core features or custom plugins

**Prerequisites:**

- Local TT-RSS clone at `${TTRSS_REPO_DIR}` (e.g., `~/dev/tt-rss`)
- vf_scored plugin cloned in `${TTRSS_REPO_DIR}/plugins.local/vf_scored`

**Starting the stack:**

```bash
vessel rss up --dev
```

**Stopping the stack:**

```bash
vessel rss down
```

**Updating the stack (sync fork, rebuild):**

```bash
vessel rss up --dev --pull   # syncs fork, stops, pulls, builds, starts
```

**Installing plugins in Dev Mode:**

Plugins must be installed in the local repository's `plugins.local/` directory:

```bash
cd ${TTRSS_REPO_DIR}/plugins.local/
git clone https://github.com/username/plugin-name.git
```

The `vf_scored` plugin should already be cloned at `${TTRSS_REPO_DIR}/plugins.local/vf_scored`.

### Configured Plugins

- [vf_scored](https://github.com/andreoliwa/tt-rss-plugin-vf-scored) - Custom keyword-based scoring plugin (available in
  dev mode, installable in normal mode via `vessel rss setup --plugin`)
- [ttrss-af-notifications](https://github.com/supahgreg/ttrss-af-notifications) - Adds a filter action to receive
  JavaScript-based notifications

## Running Locally

See the [Usage Modes](#usage-modes) section above for detailed instructions on starting/stopping the stack.

**After starting:**

1. Open **http://localhost:8002/tt-rss** → login with admin credentials
2. **Enable plugins:**
    1. Click hamburger menu (☰) or username in top right
    2. Go to **Preferences**
    3. Click **Plugins** tab
    4. Enable: `af_readability`, `fever`, `share`, `vf_scored` (if installed), etc.
    5. For first-party plugins not bundled: use built-in plugin installer in Preferences → Plugins
3. Add feeds from **http://rsshub/** (RSSHub proxy):
    1. `http://rsshub/telegram/channel/<channel>`
    2. `http://rsshub/twitter/user/<handle>`
    3. `http://rsshub/substack/<site>`
    4. `http://rsshub/youtube/channel/<id>`
    5. `http://rsshub/reddit/subreddit/<name>`
    6. Browse all routes: http://localhost:8006/ or [RSSHub Documentation](https://docs.rsshub.app/)
    7. Use [RSSHub Radar](https://github.com/DIYgod/RSSHub-Radar) browser extension to discover feeds on any website
4. On Android (same Wi-Fi), open `http://<mac-lan-ip>:8002/tt-rss` in the browser or:
    1. Use the **Tiny Tiny RSS** Android app
    2. Use **Fiery Feeds** or **Reeder** via the **Fever** plugin:
        - Server: `http://<mac-lan-ip>:8002/plugins/fever/`
        - Username/Password: your TT-RSS credentials

## Migrating to another server

1. Export feeds as OPML: **Preferences → Feeds → OPML export**.
2. Stop the stack:
    ```bash
    vessel rss down
    ```
3. Back up the database (source server).
    - Follow the dump instructions in [the PostgreSQL container](../postgres/README.md):

    ```bash
    vessel postgres dump ttrss --version 17
    ```

    - The list of recent dump files with timestamps will be displayed.

4. Back up config and RSSHub environment (source server)

    ```bash
    tar --no-xattrs -czf ~/Downloads/ttrss-config.tar.gz -C ${VESSEL_DATA_DIR}/rss config
    ```

    - RSSHub is stateless — its configuration lives entirely in environment variables inside `rss/compose.yaml` (e.g.
      `ACCESS_KEY`, `PROXY_URI`, cookies, tokens).
    - The repository itself is what you need to transfer. Redis is a cache only
      and does not need to be backed up.

5. Transfer the dump file, the config archive, and this repository to the new server (e.g. via `scp` or `rsync`).
6. Start PostgreSQL on the new server.
    - follow the [prerequisites](#prerequisites) to export all required environment variables, then:
    ```bash
    docker compose -f postgres/compose.yml up -d
    ```
7. Create the TT-RSS database and user:
    ```bash
    vessel rss setup --database
    ```
8. Restore the database dump (new server)
    - Follow the restore instructions in [the PostgreSQL container](../postgres/README.md).
      In summary:
        1. Copy the dump file into the running container or to a path accessible via a bind mount.
        2. Connect to the container and drop/recreate the database if it already has tables.
        3. Restore:
        ```bash
        vessel postgres connect ttrss --version 17 --psql --command="\i /path/to/dump.sql"
        # or directly via docker exec:
        docker exec -i postgres17 psql -U postgres ttrss < /path/to/dump.sql
        ```
9. Restore config data (new server)

    ```bash
    mkdir -p ${VESSEL_DATA_DIR}/rss
    tar -xzf ttrss-config.tar.gz -C ${VESSEL_DATA_DIR}/rss
    ```

    - RSSHub config is already in `rss/compose.yaml` (transferred in step 2). Redis will repopulate itself on first use.

10. Update URLs and expose publicly (optional)
    - Before starting, set the `TTRSS_SELF_URL_PATH` environment variable:

    ```bash
    export TTRSS_SELF_URL_PATH=https://news.yourdomain.tld/tt-rss
    ```

    - For public HTTPS access, put services behind **Caddy/Traefik** and map:
        - `http://localhost:8002/tt-rss` → `https://news.yourdomain.tld/tt-rss`
        - `http://localhost:8006/` → `https://rsshub.yourdomain.tld/`
    - If some RSSHub routes need it, configure **PROXY_URI** or cookies per docs.

11. Start the RSS stack (new server)

    ```bash
    vessel rss up         # normal/production mode
    vessel rss up --dev   # dev mode (requires TTRSS_REPO_DIR)
    ```

    - Verify at `https://news.yourdomain.tld/tt-rss`. Re-enable plugins under **Preferences → Plugins**.

## Notes

- Some RSSHub routes (Twitter/X, Instagram, YouTube) may require **proxies, cookies, or tokens** to be reliable due to
  rate limits and anti-bot measures.
- Keep Redis enabled for caching; it reduces load and speeds up feeds.
- Back up volumes: `${VESSEL_DATA_DIR}/rss/{config,redis}`, `${TTRSS_REPO_DIR}`, and PostgreSQL database.
- Export OPML from TTRSS for feed portability.
- TT-RSS uses rolling releases (no semantic versioning), so `latest` tag is safe to use.

## Alternatives considered (and why discarded)

<!-- keep-sorted start skip_lines=2 case=no -->

| App                                                    | Languages           | Self-hosted    | Extensions/Plugins                | Article Scoring            | Sort by Score | Global Filters        | Auto-labeling | Keyword/Regex Filtering | Comparison Date | Notes                                                                                                                                                                                                                                                                                                                                                                   |
| ------------------------------------------------------ | ------------------- | -------------- | --------------------------------- | -------------------------- | ------------- | --------------------- | ------------- | ----------------------- | --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Feeder](https://feeder.co/)                           | N/A                 | ❌ No (hosted) | Unknown                           | Unknown                    | Unknown       | ✅ Yes (advanced)     | Unknown       | ✅ Yes (advanced)       | 2025-11-30      | Commercial hosted service. Advanced filters and notifications. Not self-hosted; emphasis on monitoring and workflows; paid features.                                                                                                                                                                                                                                    |
| [Feedly](https://feedly.com/)                          | N/A                 | ❌ No (hosted) | ✅ Yes                            | Unknown                    | Unknown       | Unknown               | Unknown       | Unknown                 | 2025-11-30      | Not self-hosted; emphasis on social/trending; paid features for some workflows.                                                                                                                                                                                                                                                                                         |
| [FreshRSS](https://github.com/FreshRSS/FreshRSS)       | PHP                 | ✅ Yes         | ✅ Yes (but none provide scoring) | ❌ No                      | ❌ No         | ❌ No (per-feed only) | ❌ No         | ❌ No                   | 2025-11-30      | Modern UI, active development, good mobile support, cleaner codebase than TTRSS. Fatal limitations: No scoring system, no sort by score, no global filters, no auto-labeling. Feature requests pending since 2021 ([#3337](https://github.com/FreshRSS/FreshRSS/discussions/3337)). Excellent for chronological reading but cannot do keyword-based importance ranking. |
| [Fusion](https://github.com/0x2E/fusion)               | Go, TypeScript      | ✅ Yes         | ✅ Yes (open plugin system)       | ❌ No                      | ❌ No         | ❌ No                 | ❌ No         | ❌ No                   | 2025-11-30      | Lightweight (~80MB memory), single binary + SQLite. Group, bookmark, search, OPML import/export, PWA, keyboard shortcuts. Supports RSS/Atom/JSON. No scoring, filtering, or auto-labeling capabilities.                                                                                                                                                                 |
| [Miniflux](https://miniflux.app/)                      | Go                  | ✅ Yes         | Limited                           | Limited                    | Limited       | Limited               | Limited       | Limited                 | 2025-11-30      | Love the simplicity and Go performance, but advanced UI-managed filtering/scoring is more limited for this use case.                                                                                                                                                                                                                                                    |
| [NewsBlur](https://github.com/samuelclay/NewsBlur)     | Python, Objective-C | ✅ Yes         | Unknown                           | ✅ Yes (training features) | ✅ Yes        | Unknown               | Unknown       | Unknown                 | 2025-11-30      | Strong training features, but heavier and less customizable in the way I want (my keywords over social/trending signals).                                                                                                                                                                                                                                               |
| [RSS-Bridge](https://github.com/RSS-Bridge/rss-bridge) | PHP                 | ✅ Yes         | N/A                               | N/A                        | N/A           | N/A                   | N/A           | N/A                     | 2025-11-30      | Feed generator, not a reader. Lighter than RSSHub but less comprehensive and more manual per-source tuning.                                                                                                                                                                                                                                                             |
| [selfoss](https://selfoss.aditu.de/)                   | PHP                 | ✅ Yes         | ✅ Yes (open plugin system)       | ❌ No                      | ❌ No         | ❌ No                 | ❌ No         | ❌ No                   | 2025-11-30      | Lightweight (~25MB), multi-source aggregator. MySQL/PostgreSQL/SQLite support, OPML import, RESTful JSON API, extensible plugins. No scoring, filtering, or auto-labeling capabilities for keyword-based importance ranking.                                                                                                                                            |
| [yarr](https://github.com/nkanaev/yarr)                | Go                  | ✅ Yes         | ❌ No                             | ❌ No                      | ❌ No         | ❌ No                 | ❌ No         | ❌ No                   | 2025-11-30      | Minimalist, lightweight with single binary + SQLite, desktop app with tray icon, Fever API support. Explicitly [not designed for archiving](https://github.com/nkanaev/yarr/blob/master/doc/rationale.txt), focused on simple chronological reading. Completely lacks keyword-first intelligence features.                                                              |

<!-- keep-sorted end -->
