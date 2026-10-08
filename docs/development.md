# Development

## Running locally without Docker

Needs Python 3.11 or newer. Without `DATABASE_URL` the app uses a local SQLite
file (`cognex.db`), so no Postgres is needed.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
POLL_ENABLED=false uvicorn app.main:app --reload
```

Leave out `POLL_ENABLED=false` to run the background poller too.

## Tests

```bash
pytest -q
```

Or run them inside the Docker image, with no local Python setup:

```bash
docker build -t scrap-meter:test .
docker run --rm -v "$(pwd)/tests:/srv/tests" -e POLL_ENABLED=false \
  scrap-meter:test python -m pytest -q /srv/tests
```

On Windows Git Bash, prefix the `docker run` with `MSYS_NO_PATHCONV=1` and use
`$(pwd -W)` instead of `$(pwd)`.

The FTP backup tests run against a real FTP server from `pyftpdlib`, which is
not an app dependency; they are skipped unless it is installed
(`pip install pyftpdlib`, inside the container before `pytest`).

The tests cover the counter logic, production state, the SLMP frames, the OPC UA
client (against `tests/opcua_sim.py`), the TCP and UDP listeners, the
statistics, and the API end to end. They use simulated devices only.

## Project layout

```
app/
  main.py            FastAPI app, startup, schema creation, poller start
  config.py          settings from environment variables
  database.py        engine, session, schema creation and column migration
  dbconfig.py        database choice saved by the Database tab
  settings_store.py  copy of the settings files in the database (survives updates)
  backup.py          backup file format, writing and restoring all data
  backup_ftp.py      FTP/FTPS upload and the automatic backup schedule
  models.py          User, Device, Station, SourceState, Job, CounterState, Reading,
                     StationComment, Notification*, Meta
  counters.py        reset-proof counter deltas (pure, unit-tested)
  stations.py        stations and their sources: start rule, counting while in
                     production, readings, manual entries, active devices, the
                     upgrades of 1.4 data (devices became stations) and of
                     1.5-1.7 data (roles became sources)
  jobs.py            the jobs list (Jobs tab), cycle times per job (seconds per
                     shot of N pieces), the upgrade of 1.6 data (station cycle
                     times moved to the jobs) and of 1.14 cycle times (per shot)
  production.py      running / idle / stopped state per station
  scrap_stats.py     scrap statistics for a date range, previous period, alert days, Excel export
  oee.py             OEE and OK / NOK totals over the last hours (dashboard)
  comments.py        station comments with their snapshot of the station
  hmi.py             HMI windows: address checks and the server-side check
                     of a device's framing headers (X-Frame-Options, CSP)
  reporting.py       powerbi_* views and the read-only login for reports
  rawdb.py           Raw data tab: table rows, type-checked edits with the audit
                     log (DbAuditLog) and undo, the read-only SQL box
  poller.py          background poll loop and one-shot poll
  notifications.py   rule conditions, evaluation, events and routing to providers
  commands.py        WhatsApp group commands ("!status"): parsing, replies, log
  chatroom.py        the Chat room tab: one WhatsApp group or Telegram chat for the web
  auth.py            password hashing and signed session cookies
  dependencies.py    login and permission checks
  seed.py            first admin account
  templating.py      Jinja2 setup, cache-busted static URLs
  protocols/         one file per device protocol (opcua.py: OPC UA client)
  notifiers/         one file per notification transport (whatsapp, telegram;
                     whatsapp_linked runs the linked-phone client process)
  routers/           auth, account, users, devices, stations, comments, data,
                     notifications, commands, database_admin, backup_admin, rawdb, pages
  templates/         dark-mode Jinja2 pages
  static/            style.css, app.js
deploy/              compose file for a prebuilt image, and the optional
                     docker-compose.powerbi.yml (database port for reports)
tests/               pytest suite; opcua_sim.py is an OPC UA simulation server
```

Static files are linked as `/static/<file>?v=<content hash>`, so browsers load
the new `app.js` and `style.css` after every update instead of a cached copy.

## Adding a protocol

1. Create `app/protocols/<name>.py` with a class derived from
   `ProtocolDriver` (`app/protocols/base.py`). Set `key`, `label` and
   `config_fields`, and implement `read()` returning a `counters.Sample`
   (job name plus raw pass/fail counters). Raise `ProtocolError` on failure.
   Stations then see the values `pass`, `fail`, `count` and `job`. A protocol
   that can read any named value (like OPC UA) sets `any_value = True` and
   overrides `read_values(keys)` to return {key: value} for the keys its
   stations use.
2. Register the class in `_DRIVERS` in `app/protocols/__init__.py`.
3. For a push protocol, set `push = True` and follow `tcp_listener.py`.

The UI picks up the new protocol and its config fields automatically.

## Publishing an image

Multi-architecture build and push:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t <your-registry>/scrap-meter:latest --push .
```
