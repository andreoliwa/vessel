# Immich

Self-hosted photo and video management — used for images/memes (not documents; use Paperless for those).

- UI: http://localhost:2283
- Server image: `ghcr.io/immich-app/immich-server:release`
- Database image: `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0`

## Dependencies

- `redis` network + shared instance (DB index 1; index 0 is zammad)
- `${VESSEL_DATA_DIR}/immich/library` — bind-mounted upload dir (back this up)
- `IMMICH_DB_PASSWORD` env var
- `VESSEL_DATA_DIR` env var

Note: Immich uses its **own bundled Postgres 14** container (`immich-db`) rather than the
shared `postgres17` instance, because Immich requires the `pgvectors` and `VectorChord`
extensions and no PG17 variant of the immich postgres image is published.

## Setup (first time)

```bash
export IMMICH_DB_PASSWORD=<your-password>
invoke immich-setup
```

## Start / Stop

```bash
invoke immich-up           # start + follow logs
invoke immich-up --pull    # pull latest images first
invoke immich-down         # stop
```

## Storage

| Data                 | Location                            | Backed up?                                             |
| -------------------- | ----------------------------------- | ------------------------------------------------------ |
| Uploaded originals   | `${VESSEL_DATA_DIR}/immich/library` | Yes — back up this dir                                 |
| Thumbnails / encoded | `immich-thumbs` Docker volume       | No — regeneratable                                     |
| Database             | `immich-db` Docker volume           | Yes — `docker exec immich-db pg_dump -U immich immich` |

## Hetzner migration notes

On Hetzner (2vCPU/4GB VM), resource limits are already set in `compose.yaml` (0.5 CPU / 512 MB each).

Migration steps:

1. `rsync -v immich/ rlyeh:~/dev/me/vessel/immich/`
2. Also rsync updated `tasks.py` and `redis/compose.yaml`
3. Run `invoke immich-setup` on Hetzner (directory creation)
4. Transfer library: `rsync -av ${VESSEL_DATA_DIR}/immich/ rlyeh:~/data/immich/`
5. Transfer DB: dump locally, restore on Hetzner via `docker exec -i immich-db psql -U immich immich`

ML container (face recognition / CLIP search) can be added later. Add swap before enabling.
