# PostgreSQL

## Dump a single database

To dump a database on the backup dir, with date/time on the file name:

```bash
vessel --dry postgres dump [database_name]
```

## Restore a single database

To restore a database dump:

1. Connect to the container.
    ```bash
    db exec postgres14 psql -U postgres
    ```
2. Drop and recreate the database.
    ```sql
    DROP DATABASE [database_name];
    CREATE DATABASE [database_name];
    GRANT ALL PRIVILEGES ON DATABASE [database_name] TO [user_name];
    ```
3. Exit the container and restore the dump on the newly created database
   (`-d [database]` is optional: use it if the name of the database is not the same as the user).
    ```bash
    db exec -T postgres14 psql -U [user_name] -d [database] -f /var/backups/path/to/dump_of_a_single_database.sql
    ```

## Restore a database dump

Use the `db-restore` invoke task. It handles both plain `.sql` and `.sql.gz` files,
copies the dump into the backup volume if needed, drops and recreates the database,
and streams the restore — no manual steps required.

```bash
vessel postgres restore --file ~/redmine_2026-03-15-20-45-26.sql.gz \
    --database redmine_migration --role redmine --version 17
```

If the target database already exists, the task will ask for confirmation before dropping it.

## Upgrade Postgres

[How to Upgrade PostgreSQL in Docker and Kubernetes - CloudyTuts](https://www.cloudytuts.com/tutorials/docker/how-to-upgrade-postgresql-in-docker-and-kubernetes/)
