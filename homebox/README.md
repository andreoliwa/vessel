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

## Updates

The image uses the floating `latest` tag. Review the upstream HomeBox release
and back up your data before updating, then run:

```bash
vessel homebox up --pull
```

This pulls the latest image and recreates the container if the image changed.
