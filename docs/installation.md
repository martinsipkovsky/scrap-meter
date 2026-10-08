# Installation and deployment

Scrap Meter runs as two containers: the web app and a PostgreSQL database.
There are two ways to get the app image:

1. **Build it from this source code** (works for everyone).
2. **Pull a prebuilt image** from a Docker registry (only if you have access to
   one, see below).

## Requirements

- Docker Engine 24+ with the Compose plugin (`docker compose`), on Linux,
  Windows or macOS. Images build for `linux/amd64` and `linux/arm64`.
- Network access from the server to the devices (for polled protocols) and
  from the devices to the server (for listener protocols).

## Option 1: build from source

```bash
git clone <this repository> scrap-meter
cd scrap-meter
cp .env.example .env
```

Edit `.env` and set at least:

| Variable | Set it to |
|---|---|
| `SECRET_KEY` | A long random string (e.g. `openssl rand -hex 32`) |
| `POSTGRES_PASSWORD` | A password for the bundled database |
| `DEFAULT_ADMIN_PASSWORD` | The first admin's password (only used on first start) |

Then build and start:

```bash
docker compose up -d --build
```

The app is at `http://<server>:8000` (change the host port with `WEB_PORT`).
To update later, pull the new source and run the same command again.

The root `docker-compose.yml` has both `build: .` and an `image:` name. With
`--build` it builds locally and tags the result with that name; nothing is
pushed anywhere.

## Option 2: prebuilt image

`deploy/docker-compose.yml` runs a prebuilt image without any source code on
the server. It points at `azanar666/scrap-meter:latest`, which is a
**private** Docker Hub repository; only accounts given access can pull it.
Each release is also tagged with its version, e.g. `azanar666/scrap-meter:1.4.0`.
To use your own registry instead, build and push the image yourself and change
the `image:` line:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t <your-registry>/scrap-meter:latest --push .
```

On the server, put `deploy/docker-compose.yml` and `deploy/.env.example` in one
folder, then:

```bash
cp .env.example .env          # set SECRET_KEY, POSTGRES_PASSWORD, admin password
docker login                  # only needed for a private repository
docker compose up -d
```

`pull_policy: always` makes `docker compose up -d` fetch the newest image each
time, so updating the server is the same command.

## Updating

```bash
docker compose pull
docker compose up -d
```

An update replaces only the app container. Everything else is kept:

- users, devices, counters, readings and notification rules are in the
  PostgreSQL database (`db_data` volume, or the server chosen on the Database
  tab);
- the WhatsApp login is in the same bundled PostgreSQL database;
- the database choice and the FTP backup settings are files in `/srv/data`
  (the `app_data` volume) **and** a copy in the bundled PostgreSQL database.
  If the files are missing when the new container starts (a compose file
  without the `app_data` volume, or `docker compose down` and `up`), the app
  puts them back from the copy; the log says "restored it from the database
  copy".

If a saved database server is not reachable when the app starts, it retries
for about a minute before falling back to the bundled database (the Database
tab then says so). `GET /healthz` shows the running version.

### Updating to 1.13 (HMI windows)

Nothing has to be done: the compose file does not change. Stations get an
empty HMI windows list (a new database column, added on the first start).

### Updating to 1.12 (pictures and pieces)

Nothing has to be done: the compose file does not change. Jobs have no piece
rule until one is set, so counting stays as it was.

### Updating to 1.11 (Power BI from the Database tab, daily data)

Nothing has to be done: the compose file does not change. The daily data is
computed for every past day shortly after the first start (in the background).
Power BI access is now switched on on the Database tab; a server that uses
`docker-compose.powerbi.yml` keeps working as before (see
[Reports](reporting.md#setting-it-up-on-the-server)).

### Updating to 1.6 (comments, Power BI)

Nothing has to be done: the compose file does not change, and the new
comments table and the `powerbi_*` views are made on the first start. To read
the data with Power BI, see [Reports](reporting.md#setting-it-up-on-the-server).

### Updating to 1.5 (stations)

Version 1.5 separates devices (connections) from stations (what is counted).
On its first start it gives every existing device its own station with the
same id, name, settings, counters, readings and alert rules, so the dashboard
looks as before; nothing has to be done. The compose file does not change for
1.5. Take a backup first anyway (Database tab, **Download backup**). The
upgrade runs once per database, and again when a backup from an earlier
version is imported.

### Coming from Cognex Monitor (1.3 and earlier)

The app was renamed to Scrap Meter in 1.4.0 and its image moved from
`azanar666/cognex-monitor` to `azanar666/scrap-meter`. To update, change only
the `image:` line under `web:` in the server's compose file:

```yaml
    image: azanar666/scrap-meter:latest
```

then `docker compose pull` and `docker compose up -d` as usual. Keep the
folder, the service names (`db`, `web`) and the volume names as they are, so
the same database and settings are used; nothing has to be moved. The
database name and user (`cognex`) also stay. Versions 1.4 and 1.5 were
published under the old image name too.

### Before 1.2.0

Versions before 1.2.0 did not keep that copy. If your server's compose file has
no `app_data` volume (check for `app_data:/srv/data` under `web:`), copy the
current `deploy/docker-compose.yml` before updating; otherwise the database
choice and FTP settings have to be entered once more after this one update,
and are kept from then on.

## Ports and firewall

| Port | Protocol | Used by |
|---|---|---|
| `8000` (`WEB_PORT`) | TCP | Web UI and API |
| `5100-5119` (`LISTEN_PORTS`) | TCP and UDP | Devices that push data: TCP listener, UDP listener, SLMP server. One port per device. |
| `5119` (one of `LISTEN_PORTS`, chosen on the Database tab) | TCP | Optional, while Power BI access is on: the read-only login for Power BI / Excel. Allow it only from the Power BI or gateway PC. |
| `5432` (`POWERBI_DB_PORT`) | TCP | Only with the older `docker-compose.powerbi.yml`: the database, for Power BI / Excel. |

Compose publishes the whole listener range on both TCP and UDP. Open it in the
host firewall too, for example:

```bash
sudo ufw allow 5100:5119/tcp
sudo ufw allow 5100:5119/udp
```

Polled protocols (OPC UA, Data Channel, Modbus, Native Mode, SLMP client,
PROFINET gateway) need no inbound ports; the app connects out to the device,
PLC or OPC UA server (usually port 4840 for OPC UA).

If you change `LISTEN_PORTS`, the app and the published ports both follow it,
since compose uses the same variable for each.

## Data and backups

Two Docker volumes hold everything that must survive an update:

| Volume | Contents |
|---|---|
| `db_data` | The bundled PostgreSQL database: users, devices, counters, readings, comments, alerts |
| `app_data` | `/srv/data` in the app container: the database choice and FTP backup settings saved on the Database tab (also copied into `db_data`), backups taken before an import, and the OPC UA client certificate |

The easiest backup is on the Database tab: **Download backup**, or the
automatic backup to an FTP server (see [Configuration](configuration.md#backups-database-tab)).
A backup file from there is restored with **Import backup** on the same tab.

You can also back up the bundled PostgreSQL database directly:

```bash
docker compose exec db pg_dump -U cognex cognex > cognex-backup.sql
```

`docker compose down` keeps the volumes; `docker compose down -v` deletes
them, and all data with them.

## First login

Sign in with `DEFAULT_ADMIN_USER` / `DEFAULT_ADMIN_PASSWORD` (default
`Admin` / `1234`). These are only used to create the first account on an empty
database; changing them later has no effect. Change the password under
*Account* and create other users under *Users*.

## Health check

`GET /healthz` returns 200 and the app version when the app is up. The image's Docker
`HEALTHCHECK` uses it.
