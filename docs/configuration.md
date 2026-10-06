# Configuration

## Environment variables

Set these in `.env` next to `docker-compose.yml`. Compose reads it
automatically. When running without Docker, the app also reads `.env` from the
working directory.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | local SQLite file `./cognex.db` | SQLAlchemy URL. Compose sets it to the bundled Postgres container. |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `cognex` / `cognex` / `cognex` | Credentials for the bundled Postgres container (compose only) |
| `SECRET_KEY` | `change-me-in-production` | Signs login session cookies. **Change it.** |
| `DEFAULT_ADMIN_USER` / `DEFAULT_ADMIN_PASSWORD` | `Admin` / `1234` | First admin account, created only when the database has no users |
| `POLL_ENABLED` | `true` | Run the background poller. Set `false` for tests or a read-only instance. |
| `WEB_PORT` | `8000` | Host port for the web UI (compose only) |
| `LISTEN_PORTS` | `5100-5119` | Port range, TCP and UDP, for devices that push data |
| `DATA_DIR` | `./data` (`/srv/data` in Docker) | Where the Database tab saves its settings (database choice, FTP backup settings, backups taken before an import), and the OPC UA client certificate (`opcua/`) |
| `WHATSAPP_ENABLED` | `true` | Reconnect a linked WhatsApp phone when the app starts (see [Notifications](notifications.md#linked-send-from-your-own-number-no-extra-service)) |

Login sessions last 12 hours.

## Database tab (admins only)

The *Database* tab shows which database the app is using and lets an admin
switch to another PostgreSQL server:

1. Enter the new server's connection details and press **Test**.
2. Optionally tick the option to **copy all current data** into it. The target
   database must be empty for this.
3. **Save**, then **Restart** the app so it reconnects.

The tab also shows ready-to-copy `docker run` and `docker compose` snippets for
starting a new PostgreSQL container.

The choice is saved in `DATA_DIR` (the `app_data` volume), with a copy in the
`DATABASE_URL` database, so it survives image updates (see
[Updating](installation.md#updating)). If the saved database can't be reached
at startup, the app retries for about a minute and then falls back to
`DATABASE_URL` so you can still log in and fix it. Removing the saved
setting returns the app to `DATABASE_URL` after a restart.

## Backups (Database tab)

**Download backup** saves one file, `cognex-backup-DATE-TIME.json.gz`, with all
app data: devices, readings, counters, users and notification settings. It is
written by the app itself (no `pg_dump`), so it can be imported into any
database the app is configured to use. The tab shows when the last backup was
made (downloaded or uploaded to FTP) and turns yellow when there is none or
the newest is older than 7 days.

**Import backup** replaces all current data with the file:

1. Choose the file. The app checks that it is a complete Scrap Meter backup (backups
   from earlier versions, called Cognex Monitor, are accepted)
   and shows what it contains. Nothing is changed yet.
2. Tick the confirmation and press **Replace all data with this backup**.
3. The app first saves the current data as a backup in `DATA_DIR/backups`
   (the last 5 are kept and listed on the tab for download), pauses device
   polling, replaces everything in one transaction and resumes polling. If the
   import fails, the current data is left unchanged.
4. Everyone signs in again with the accounts from the backup.

To undo an import, import the file saved before it.

### Automatic backup to FTP

Fill in the FTP server, port, user, password and remote folder (created if
missing). Tick **FTPS** for explicit TLS (`AUTH TLS` on port 21); the server's
certificate is not checked, so self-signed certificates work. Choose a
schedule, either daily at a time (in the time zone of the browser that saved
it) or every N hours, and how many backups to keep on the server.

- **Test connection** logs in and writes and deletes a small test file.
- **Run now** makes a backup and uploads it straight away.
- Each run uploads one file and then deletes the oldest
  `cognex-backup-*.json.gz` files in the folder beyond the number to keep.
  Other files in the folder are never touched.
- The result of the last run, including any error, and the time of the next
  run are shown on the tab. A daily run that was missed while the app was
  stopped runs as soon as it is back.

Rules on the Notifications tab can send a message when a backup fails or
finishes.

The app connects out to the FTP server in passive mode, so no inbound port is
needed. The FTP password is stored in `DATA_DIR/backup_ftp.json` and copied
into the `DATABASE_URL` database (table `app_settings`), like the database
password; protect the `app_data` and `db_data` volumes accordingly.

## Schema changes

The app creates its tables on startup and adds new columns to an existing
database automatically when an update needs them. There is no separate
migration step.
