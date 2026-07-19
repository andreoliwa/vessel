# Redmine

- Docker Hub: https://hub.docker.com/_/redmine
    - https://github.com/docker-library/redmine
    - Docs: https://github.com/docker-library/docs/tree/master/redmine
- GitHub: https://github.com/redmine/redmine

## Email Configuration

### Outgoing email (SMTP)

Configured in `configuration.yml` under `default.email_delivery`. The current setup uses Gmail SMTP with STARTTLS on port 587. Fill in `user_name` and `password` (use a Gmail App Password if 2FA is enabled).

### Incoming email (IMAP polling)

Email receiving is implemented as a Rails initializer (`check_email.rb`) using `rufus-scheduler`. It polls Gmail IMAP every minute inside the Rails process — no separate cron job or container needed. It reuses the SMTP credentials (`user_name`/`password`) from `configuration.yml` for the IMAP connection, plus the `imap_project` key at the top of `configuration.yml` to route emails to the correct Redmine project.

The `check_email.rb` initializer is baked into the Docker image via the `Dockerfile` (`COPY ./check_email.rb config/initializers/check_email.rb`). `rufus-scheduler` is also added to the bundle in the `Dockerfile`.

**To disable incoming email**, remove `check_email.rb` from `config/initializers/` and rebuild the image:

```bash
# In redmine/Dockerfile, remove or comment out:
# COPY ./check_email.rb config/initializers/check_email.rb

docker compose build
docker compose up -d
```

Alternatively, leave the file in place but set `imap_project` to a blank/invalid value in `configuration.yml` — IMAP will still connect but emails won't be routed anywhere.

# Setup

1. Copy `configuration.sample.yml` to `configuration.yml` and fill in the variables.
   Or copy the YAML file from the live production server.
2. Spin up the [shared PostgreSQL](../postgres/docker-compose.yml) instance with `db up -d`
3. Connect to the database with one of these commands:
    - `pgcli postgresql://postgres:$POSTGRES_PASSWORD@localhost:7714`
    - `db exec postgres14 psql -U postgres`
4. Create the Redmine user with:
    ```sql
    CREATE USER redmine;
    ALTER USER redmine WITH PASSWORD '<type the value of REDMINE_DB_PASSWORD here>';
    CREATE DATABASE redmine;
    GRANT ALL PRIVILEGES ON DATABASE redmine TO redmine;
    ```
5. Connect to the database with the newly created userone of these commands:
    - `pgcli postgresql://redmine:$REDMINE_DB_PASSWORD@localhost:7714`
    - `db exec postgres14 psql -U redmine`
6. [Restore the database if you have a backup](../postgres/README.md)
7. Copy files from one server to the other, e.g. `scp -r old-server:/path/to/Redmine new-server:/path/to/`
8. Spin up Redmine with `redmine up` or `redmine up -d`
9. Wait for the server to come up, then [login with the default admin/admin user/pass](https://github.com/docker-library/docs/tree/master/redmine#accessing-the-application) or with an existing user
10. Start using Redmine: Change the password, create users, create projects...
11. Make sure the backups are working and running on the crontab: `~/dev/me/vessel/redmine/backup_redmine.sh`
