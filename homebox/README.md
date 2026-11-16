# HomeBox

Local [HomeBox](https://homebox.software/) proof of concept for manually
assessing its built-in home-inventory interface.

## Start and use

Set `VESSEL_DATA_DIR` to a private local data directory before starting:

```bash
export VESSEL_DATA_DIR="$HOME/data"
```

`VESSEL_DATA_DIR` must not point inside this repository.

Before starting, create `homebox/.env` yourself with the random API-key
pepper required by HomeBox 0.26+. This file is Git-ignored; Vessel does not
create it automatically. Keep it private and retain it when updating or
recreating the container.

```bash
vessel homebox up --pull
vessel homebox ps
vessel homebox logs
```

Open <http://localhost:8009> and create the initial local account.

The `${VESSEL_DATA_DIR}/homebox` directory is private, outside Git, and
contains HomeBox's SQLite state and uploaded files. Back it up using your
private backup process.

Stop or remove the container without deleting its data:

```bash
vessel homebox stop
vessel homebox down
```

## Due maintenance

Set `HOMEBOX_API_KEY` to a HomeBox API key, then list incomplete maintenance due today or earlier:

```bash
vessel homebox maintenance-due
```

The Rich table shows each item's link, title, maintenance date, and days overdue, oldest first.
Entries due today show `Today`; other entries show `1 day`, `2 days`, and so on.
Dates use the machine's local calendar date.
Each due maintenance entry gets its own row, including multiple entries for the same item.
The command only reads data and does not mark maintenance complete.
Set `HOMEBOX_URL` to use another HomeBox instance; the default is `http://localhost:8009`.
Use `vessel --dry homebox maintenance-due` to preview without credentials or API requests.

Use `--json` for a JSON array instead of the table:

```bash
vessel homebox maintenance-due --json
```

Each object has `item_url`, `item_title`, `maintenance_date` (`YYYY-MM-DD`), and `overdue` (`Today`, `1 day`, or `N days`).
No due entries returns `[]`.
With `--dry --json`, stdout contains `[]` and the dry-run notice goes to stderr.

## Updates

The image uses the floating `latest` tag. Review the upstream HomeBox release
and back up your data before updating, then run:

```bash
vessel homebox up --pull
```

This pulls the latest image and recreates the container if the image changed.
