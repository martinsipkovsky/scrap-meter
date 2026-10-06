# Scrap Meter

A self-hosted web app that tracks scrap and production across a plant in one
place. It reads pass/fail counters from the plant's devices (Cognex vision
cameras, PLCs, OPC UA servers and other counters), by polling them or by
letting them push their results, and counts them per **station**: a station
takes its OK, NOK and job from one or several devices (or from manual
entries). It keeps **reset-proof running totals** per station and job, shows
OEE, logs everything to a database, and sends alerts to WhatsApp or Telegram
groups when scrap is high, a device drops off the network, or something else
you choose happens.

It is a Python/FastAPI app with a dark-mode web UI, logins with per-user
permissions, and PostgreSQL storage, shipped as a Docker image. It was called
Cognex Monitor up to version 1.3; data, backups and export files from those
versions keep working.

> **Project status: early, not yet validated on real hardware.** All
> communication protocols, including SLMP and OPC UA, have been developed and
> tested against simulators and unit tests only. None has been tested with a
> real Cognex camera, Mitsubishi PLC or production OPC UA server yet. Expect to
> adjust protocol settings, and please report what works and what doesn't. See
> [Known issues and limitations](docs/known-issues.md).

## Features

- **Many protocols, one file each:** OPC UA client (any OPC UA server, with
  security and login, and a Browse helper to pick nodes), Cognex Data
  Channel, Modbus/TCP, generic TCP / Native Mode, TCP and UDP listeners (the
  device pushes data to the app), Mitsubishi SLMP / MC protocol (as client or
  as a fake PLC the device writes to), PROFINET via a gateway, and a built-in
  simulator.
- **Stations:** the dashboard's building blocks. A station counts one or more
  sources, each a device with its own OK, NOK, total and job values: two
  cameras each running their own job, or OK from an OPC UA PLC and NOK from a
  reject counter that pushes over TCP. The dashboard shows which devices are
  active. Parts counted by hand are added as manual entries on the station.
- **OEE meter:** availability, performance and quality over the last 24 hours,
  with total OK and NOK, at the bottom of the dashboard. Ideal cycle times are
  set per job, so a station that changes job is weighed correctly.
- **Reset-proof counters:** if an operator resets the counters on the device,
  the running totals keep going. A job change freezes the old job's totals and
  starts new ones.
- **Production state:** a station goes into production when a source makes,
  say, 2 OK pieces within a minute (set per source), and leaves it when no
  source counts OK for its idle timeout. Counters only change while in
  production, so idle lines don't cause false scrap; idle stations are grayed
  out and get no scrap alerts.
- **Station view:** an OK/NOK chart over 1 h, 8 h, 24 h or 7 days, manual
  Start/Stop of production, and **Reset counters** for the dashboard counters
  (history and statistics are kept).
- **Comments:** anyone on the dashboard can write a comment on a station. It
  is saved with the time, the author, the current job and the OK / NOK / scrap
  at that moment; the latest one shows on the station's dashboard block.
- **Power BI and Excel:** read-only `powerbi_*` database views with all
  counted data and the comments, an optional read-only login, and a manual on
  the Database tab.
- **Scrap statistics:** pass, fail and scrap % for any date range, overall,
  per station, per job and per day, with an Excel export. Time out of
  production, readings a user excluded and stations set to *Exclude* (such as
  a test rig) are left out of the overall figures.
- **Alerts:** rules for scrap rate (with per-station thresholds), fail count,
  disconnects, production and job changes, backup results and app updates,
  each with a level (info / warning / alert) and its own destinations:
  WhatsApp sent from your own number (linked device, unofficial), Telegram
  bots, webhooks, Green API or Meta Cloud API.
- **WhatsApp group commands:** `!status` (or your own commands) in a group
  answers with live production state, OK / NOK and scrap per station.
- **Users and permissions:** login required, with granular permissions per user.
- **Raw data tab** for admins: every table page by page with search, filters
  and sorting, type-aware inline editing, a change log with undo, and a
  read-only SQL box. Secrets stay hidden.
- **Export/import** of devices, stations and job cycle times as JSON, and an admin **Database tab** to move the
  app to another PostgreSQL server.
- **Backups:** download all data as one file, import it again, and automatic
  scheduled backups to an FTP/FTPS server, with a reminder when the last
  backup is more than a week old.
- **Safe updates:** pulling a new image keeps all data and settings, including
  the database choice, FTP backup settings, the WhatsApp login and the OPC UA
  client certificate.

## Quick start (build from source)

You need Docker with the Compose plugin.

```bash
git clone <this repository> scrap-meter
cd scrap-meter
cp .env.example .env          # set SECRET_KEY and passwords before real use
docker compose up -d --build
```

Open <http://localhost:8000> and sign in as `Admin` / `1234`. Change that
password straight away under *Account*.

To try it without any hardware, add a device with the **Simulated device**
protocol on the Devices tab (with "Also add a station" ticked). For OPC UA, `python tests/opcua_sim.py` runs a
small simulation server (see [Protocols](docs/protocols.md#opc-ua-client-opcua)).

## Documentation

| Guide | What it covers |
|---|---|
| [Installation and deployment](docs/installation.md) | Building from source, running on a server, firewall ports, updates, backups |
| [Configuration](docs/configuration.md) | Environment variables and the Database tab |
| [User guide](docs/user-guide.md) | Devices and stations, dashboard and OEE, comments, manual entries, production state, data, statistics, users, export/import, the Raw data tab |
| [Protocols](docs/protocols.md) | How to connect each device type, with every config field |
| [Reports: Power BI, Excel and SQL](docs/reporting.md) | Reading the data with Power BI (Desktop and scheduled refresh), Excel or SQL; the views and example queries |
| [Notifications](docs/notifications.md) | Alert rules, WhatsApp (linked phone) and Telegram delivery |
| [Development](docs/development.md) | Running locally, tests, project layout, adding a protocol |
| [Known issues and limitations](docs/known-issues.md) | What is untested or doesn't work yet |

## Security notes

- The default admin password (`1234`) and `SECRET_KEY` must be changed before
  the app is reachable by anyone else.
- The app is meant for a plant network. If you expose it to the internet, put
  it behind HTTPS (a reverse proxy) and restrict access.
- The listener ports (5100-5119 by default) accept data from any sender unless
  you set the device's *Host* field to its IP.
- Publishing the database for Power BI (optional) opens its port on the
  server: allow it in the firewall only from the Power BI or gateway PC. The
  read-only login can read the `powerbi_*` views only.
- OPC UA passwords are stored in the app's database (readable by its
  administrators and in backups) but are never sent back to the browser or
  written to device export files.

## License

[MIT](LICENSE)
