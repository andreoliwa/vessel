# Zammad

Modern open-source helpdesk/ticket system. Replaces Redmine for issue tracking, email-based ticket creation, and
calendar integration.

## Prerequisites

- PostgreSQL 17 running (`cd ../postgres && docker compose up -d postgres17`)
- Shared Redis running (`cd ../redis && docker compose up -d`)
- Environment variables set:

```bash
export ZAMMAD_DB_PASSWORD=<password>
```

## Setup

```bash
# Create database and user
invoke zammad-setup

# Start the stack
invoke zammad-up

# Open http://localhost:8008
```

## Usage

```bash
invoke zammad-up              # Start and follow logs
invoke zammad-up --pull       # Pull latest images first
invoke zammad-down            # Stop
invoke zammad-fetch-emails    # Force immediate email fetch from all channels
```

## Architecture

- **zammad-init** — One-shot: runs DB migrations and Elasticsearch index setup
- **zammad-railsserver** — Main Rails application
- **zammad-nginx** — Nginx reverse proxy (port 8008)
- **zammad-scheduler** — Background job processor (Sidekiq)
- **zammad-websocket** — WebSocket server for real-time updates

### Email polling frequency

The scheduler polls all inbound email channels (IMAP) every **30 seconds** — this is a hardcoded `Scheduler` DB record
set during `zammad-init`, not a config file or env var. It is not configurable via the Admin UI. To verify the current
interval:

```bash
docker exec zammad-railsserver bundle exec rails r \
  'puts Scheduler.all.map { |s| "#{s.period}s | #{s.name}" }.sort.join("\n")'
```

The relevant entry is `Check channels. (30s)`. Changing it requires a direct DB update and would be reset on the next
`zammad-init` run (e.g. during upgrades). Use `invoke zammad-fetch-emails` for a one-off immediate fetch.

30s polling is not a meaningful resource concern: IMAP polling is IO-bound (open TCP connection, issue `IDLE`/`STATUS`,
close). CPU usage is negligible. The real resource consumers in this stack are Elasticsearch and the Rails server, both
already tuned with memory limits in `compose.yaml`.

- **zammad-elasticsearch** — Full-text search (Elasticsearch 8)
- **zammad-memcached** — Response caching

## Integrations (configured via Zammad Admin UI)

- **Email** — Channels → Email: add IMAP/SMTP channels for Gmail or Fastmail
- **iCal** — Built-in calendar feed (see [Subscribing to the iCal feed](#subscribing-to-the-ical-feed) below)
- **Telegram** — Channels → Telegram: add bot token from BotFather, requires HTTPS

### Setting up Gmail as an email channel

Two options:

**Option A: Plain IMAP with an App Password (simpler)**

Works fine if your Google account has 2FA enabled (required for App Passwords). Go to **Admin → Channels → Email → Add
Account** and use:

- Incoming: `imap.gmail.com`, port 993, SSL
- Outgoing: `smtp.gmail.com`, port 587, STARTTLS
- Username: your full Gmail address
- Password: an [App Password](https://myaccount.google.com/apppasswords) generated in your Google Account settings (
  Security → App Passwords)

**Option B: Google OAuth channel (no App Password needed)**

1. Go to [Google Cloud Console](https://console.cloud.google.com/), create an OAuth client ID (Web application type).
2. In Zammad, go to **Admin → Channels → Google → Connect Google App** — it shows the callback URL to add as an
   Authorized redirect URI in Google.
3. Paste the Client ID and Client Secret into Zammad and save.
4. Click **Add Account**, complete Google's consent screen, and configure group assignment.

**Before connecting an inbox with existing emails:**

1. Disable auto-reply triggers first: **Admin → Manage → Triggers** — disable _"auto-reply (on new tickets)"_ and _"
   auto-reply (on follow-up of tickets)"_. Otherwise, Zammad sends an auto-reply to every imported email, including old
   ones. Re-enable after the initial import.
2. During account setup, use the **Experts** dialog to enable **Archive Mode** for emails older than 2 weeks — this
   prevents old emails from being treated as new tickets.
3. To keep emails on the server rather than deleting them after import, enable **Keep Messages on Server** in the \*
   \*Experts\*\* dialog during account setup.

**After connecting the inbox, enable follow-up detection via References:**

Go to **Admin → Channels → Email → Settings → Additional follow-up detection** and enable **References**. This uses
standard `Message-ID` / `In-Reply-To` email headers to match replies to existing tickets, with no false-positive
risk. \*
\*Body** and **Attachment\*\* options are also available but can cause false detections.

### Subscribing to the iCal feed

Zammad provides a built-in iCal feed that shows pending-reminder tickets as calendar events at their `pending_time`.
To find your personal iCal URL: **Avatar (bottom-left) → Profile → Calendar**. Copy the URL and subscribe to it in any
calendar app.

Available feeds:

| Feed              | URL path                                           |
| ----------------- | -------------------------------------------------- |
| All tickets       | `https://zammad.example.com/ical/tickets`          |
| Pending reminders | `https://zammad.example.com/ical/tickets/pending`  |
| New & open        | `https://zammad.example.com/ical/tickets/new_open` |

The feed includes tickets in the new, pending, and escalation categories. It is not configurable — there is no way to
filter by specific state or custom criteria.

**Google Calendar compatibility:** Two workarounds are needed for Google Calendar:

1. **Auth bypass:** Google Calendar cannot do HTTP Basic Auth, so the iCal endpoints must be publicly accessible. The
   Caddy reverse proxy injects a Zammad API token on `/ical/*` requests via `header_up Authorization`, bypassing both
   the auth gateway and Zammad's Basic Auth. To set this up, create an API token in Zammad (**Admin → API Token**) and
   configure the Caddyfile (see the Caddy config on the server).

2. **Cache busting:** Google Calendar caches iCal feeds aggressively (12-24h) and there is no manual refresh. If you
   change the feed configuration and need Google to re-fetch immediately, remove the subscription and re-add it with a
   dummy query parameter (e.g. `?v=2`). Google treats it as a new URL, bypassing the cache.

3. **CLASS:PRIVATE rewrite:** Zammad hardcodes `CLASS:PRIVATE` on all iCal events. Google Calendar hides details for
   private events on subscribed calendars, showing only "Busy". A custom nginx template (`nginx-zammad.conf`) adds a
   `/ical` location with `sub_filter` to rewrite `CLASS:PRIVATE` → `CLASS:PUBLIC`. This file is mounted over
   `contrib/nginx/zammad.conf` so the entrypoint's sed substitutions still apply.

## Migration from Redmine

Migration is implemented as an invoke task.

### Configuration

Copy `zammad.toml.example` to `zammad.toml` (gitignored) and fill in your instance's state/priority names:

```bash
cp zammad/zammad.toml.example zammad/zammad.toml
# edit zammad/zammad.toml
```

### Before running the migration

Generate an API token in Zammad: **Admin → API Token → New API Token** — give it a name, set
**Expiry** to unlimited, and check all permissions. Export it:

```bash
export ZAMMAD_TOKEN=<token>
```

### Running the migration

```bash
# Run the migration (import mode is enabled/disabled automatically)
invoke zammad-migrate

# Rebuild the search index after migration
invoke zammad-reindex
```

All credentials can be passed as flags (`--redmine-db-pass`, `--zammad-token`) or via `POSTGRES_PASSWORD` /
`ZAMMAD_TOKEN` env vars.

### Re-importing from scratch

```bash
invoke zammad-wipe     # deletes all imported tickets, users, groups, custom fields
invoke zammad-migrate  # import mode toggled automatically
invoke zammad-reindex
```

### Post-migration verification checklist

After running the migration, verify the following manually in the Zammad UI:

- [ ] Ticket states match the Redmine statuses (Admin → Ticket States)
- [ ] Ticket priorities are correct (Admin → Ticket Priorities)
- [ ] Imported group exists with one sub-group per Redmine tracker (Admin → Groups)
- [ ] Users were imported with correct names and emails (Admin → Users)
- [ ] Custom fields appear on tickets (Admin → Objects)
- [ ] A sample of tickets has correct creation dates (requires import mode was active)
- [ ] Journal notes appear as articles on tickets
- [ ] Tickets are assigned to the correct tracker sub-group (e.g. `Redmine Import::Issue Type`)
- [ ] Parent/child ticket links are visible on tickets that had a parent issue in Redmine
- [ ] Search returns results after `invoke zammad-reindex` completes
- [ ] [Set up Gmail/email integration channel](#setting-up-gmail-as-an-email-channel) and test some use cases:
    - [ ] Create a ticket via email
    - [ ] Reply to a ticket via email
    - [ ] Create another ticket via email and merge it with an existing ticket
    - [ ] Forward an email to the channel address and verify it creates a new ticket
    - [ ] Move an existing email to the inbox and verify it appears in Zammad

## Backup

- PostgreSQL `zammad` database: extend `postgres/cron-backup.sh` (see spec Section 6)
- Elasticsearch index: rebuildable via `rake zammad:searchindex:rebuild`
- File storage: `zammad-storage` volume (minimal if attachment-free)

## Tips and Learnings

### Articles (notes/comments) are immutable

Zammad does not allow editing articles after creation — this is by design for audit trail purposes. There is no edit
button in the UI. The only way to correct a note is via a direct database update:

```bash
# Find the article
vessel postgres connect zammad --version 17 --psql --command="SELECT id, body FROM ticket_articles WHERE ticket_id = <TICKET_ID> ORDER BY id;"

# Update it
vessel postgres connect zammad --version 17 --psql --command="UPDATE ticket_articles SET body = '<corrected text>' WHERE id = <ARTICLE_ID>;"
```

### Search delay after migration

Elasticsearch takes several minutes to index all tickets after a migration. During this time, full-text search may
return no results or partial results — this is normal. To trigger reindexing manually:

```bash
invoke zammad-reindex
```

You can monitor Elasticsearch indexing progress at `http://localhost:9200/_cat/indices?v`.
