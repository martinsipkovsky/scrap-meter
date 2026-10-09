# Test report: Scrap Meter 1.22.1

Date: 2026-10-09. Tested on the development PC (Windows 11, Docker 29.8,
Docker Compose 5.5) with the image built from this repository.

## Summary

| Run | Result |
|---|---|
| Automated test suite (pytest, 28 files) | **197 passed**, 0 failed |
| End-to-end check of every tab through the app's API, on a throwaway stack with simulated devices | **55 passed**, 0 failed |
| Browser check of 18 pages in dark mode, light mode and at phone width | **54 passed**, 0 failed |

The first end-to-end run found four problems in the app. They were fixed in
1.22.1 before the final runs above (see [Fixed during this run](#fixed-during-this-run)).
Nothing was sent to real cameras, PLCs, messengers or accounts. What could not
be tested and why is listed under [Not tested](#not-tested).

## How it was tested

**Automated suite.** `pytest` inside the Docker image, on SQLite, with the
simulated devices and fake servers in `tests/` (see
[Development](development.md#tests)). It covers the counting logic, every
protocol driver against a simulator, the statistics, OEE, notifications,
commands, the Chat room, backups (with a real FTP server from pyftpdlib), the
raw data tab, settings and the Tutorial.

**End-to-end check.** `tests/e2e_run.sh` starts a separate, throwaway copy of
the app with its own PostgreSQL. It adds a week of history for 4 stations
(96,000 readings) and starts simulated devices for every protocol:

- a Modbus/TCP server, also used for the PROFINET gateway;
- a Cognex Native Mode server with login;
- a Data Channel sender;
- an OPC UA server;
- an FTP server.

TCP, UDP and SLMP records are sent to the app's listening ports. Fake Discord
and Signal servers stand in for the messengers (`tests/fake_messengers.py`).
`tests/e2e_check.py` then works through every tab as an administrator and as
a restricted user. `tests/e2e_browser.py` opens every page in Chromium. It
fails a page on a JavaScript error, a failed request, text with too little
contrast, or a page wider than a phone screen. The stack is removed
afterwards. The raw results are in [tests/e2e.json](../tests/e2e.json) and
[tests/e2e-browser.json](../tests/e2e-browser.json).

## Results per feature

| Feature | What was tested | How | Result |
|---|---|---|---|
| Sign in | API refuses calls without a login (401), pages send you to the login, a wrong password is refused, the start page after login | E2E; pytest | Pass |
| Devices: Native Mode (TCP) | Login handshake, job / pass / fail read, station counts | E2E against a simulated In-Sight; pytest | Pass |
| Devices: Data Channel | Records read, station counts | E2E simulated sender | Pass |
| Devices: Modbus/TCP, PROFINET gateway | 32-bit counters, fixed job, station counts | E2E pymodbus server | Pass |
| Devices: TCP and UDP listeners | Records sent to the app's ports, station counts | E2E; pytest (8 + 4 tests) | Pass |
| Devices: SLMP server and client | A "camera" writes D100/D102 to the app over SLMP; an SLMP client device reads them back | E2E; pytest (7 tests) | Pass |
| Devices: OPC UA | Browse the server, a station on three nodes counts | E2E OPC UA simulator; pytest (9 tests, incl. security and login) | Pass |
| Devices: simulator, poll now, export / import | All work; importing the export updates the existing devices | E2E | Pass |
| Stations | Start / stop by hand, reset counters, edit, HMI window framing check, Scrap warnings switch, chart over 1 h to 7 days | E2E; pytest | Pass |
| Dashboard | Summary of all 15 stations, problem codes W01 → E02 → W03 for a broken device, OEE per block, sound on status changes | E2E; browser; sound checked in 1.18.0 | Pass |
| Jobs | Add a job before production, cycle time per shot (12 s / 4 = 3 s per piece), piece rules | E2E; pytest (13 + 11 tests) | Pass |
| Manual entries | Entry, correction (the app asks before going below zero), edit, delete | E2E; pytest | Pass |
| Comments | Write, list, delete | E2E; pytest | Pass |
| Data log | Readings, exclude and include one, exclude a period, mute events | E2E; pytest | Pass |
| Scrap statistics | Today's figures add up (overall = sum of the counted stations), previous period, hourly trend, Excel download | E2E; pytest; same numbers before and after the 1.18.1 speed-up (22,837 values compared) | Pass |
| OEE | Figures for every station, performance from a job's cycle time | E2E; pytest | Pass |
| Notifications | Discord bot and webhook providers, Signal provider, Test send, scrap-rate and disconnect rules delivered, alert log | E2E with fake Discord / Signal; pytest (Telegram and WhatsApp with fakes) | Pass |
| Chat commands | `!status` answered in a Discord channel, `!mute` / `!unmute` from a Signal group, prefix change, `#help`, preview, command log, list of chats | E2E; pytest | Pass |
| Chat room | A Discord channel as the room: messages in and out | E2E; pytest (Telegram, WhatsApp, Signal) | Pass |
| Settings | Own settings, defaults for everyone, reset, light / system mode on every page | E2E; browser | Pass |
| Developer options | Off hides and stops WhatsApp and Signal; only administrators change them; an older server keeps what it used | E2E; pytest | Pass |
| Users and permissions | A user with only *view_dashboard* is refused data, notifications, users, database and raw data (403), cannot stop production; password change; delete | E2E; pytest | Pass |
| Database tab and backups | Backup download, import of that backup (stations, devices, jobs, users, rules unchanged afterwards), FTP backup test and run to an FTP server | E2E; pytest (7 tests) | Pass |
| Power BI | Power BI access switched on: the read-only login reads `powerbi_readings` through the app's port and is refused the `users` table; daily data rebuilt | E2E | Pass |
| Raw data | Tables, rows, edit a value, change log, undo, read-only SQL; a `DELETE` is refused | E2E; pytest | Pass |
| Tutorial and Changelog | All pages, pictures and links exist; open in the app | E2E; browser; pytest | Pass |
| Every page in a browser | 18 pages in dark, light and at 390 px phone width: no JavaScript errors, no failed requests, readable text, no sideways page scroll | Browser (Chromium) | Pass |

## Fixed during this run

The first end-to-end run failed on these. They are fixed in 1.22.1:

1. **A device that was just added showed a red `E04`** on the dashboard until
   its first read. It now shows the amber `W01` (waiting).
2. **On phones, pages with wide tables were wider than the screen**: Stations,
   Devices, Jobs, Data log, Notifications, Users and the station view. Wide
   tables now scroll sideways inside the page.
3. **The "!" marks in the Scrap statistics day map were hard to read in light
   mode.** They now have a dark outline.
4. **The user guide opened from the Tutorial was 4 px wider than a phone
   screen** (long words). It now wraps.

The other failures of the first run were mistakes in the test script itself
(wrong field names in its requests), which were corrected. The Chat room's
message times now follow the 24 h / 12 h setting; that was found while taking
the Tutorial screenshots and fixed in 1.22.0.

## Observations (not failures)

- **Performance above 100 %.** The simulated line makes pieces faster than
  the cycle time set for its job, so its OEE performance is 428 %. That is
  correct arithmetic. On a real line it means the job's cycle time is set too
  slow.
- **Listeners that connect per record.** A camera that opens a new
  connection for every record shows as disconnected (`E04`) between records.
  Its pieces are still counted. Cameras that keep the connection open don't
  show this.

## Not tested

| What | Why |
|---|---|
| Real Cognex cameras, Mitsubishi PLCs, Modbus devices, OPC UA servers | No hardware on this PC; each protocol was tested against a simulator that speaks it |
| PROFINET real-time | Not supported by the app (only through a Modbus gateway, which was tested) |
| WhatsApp virtual client with a real phone | No phone is linked on the test PC; pytest replaces the client with a fake one |
| Real Discord, Signal and Telegram accounts | Nothing is sent to real accounts without your say-so; fake servers that answer like the real APIs were used. The real signal-cli image was started once to check its answers |
| Power BI Desktop itself | Not installed here; the same connection was made with PostgreSQL's own client library |
| Your server (`deploy/docker-compose.yml`, update from your current version) | Tested on this PC only. The upgrade steps (new index, developer options, settings) run on the first start and were checked on the local stack |
| FTPS (FTP over TLS) | The FTP server in this run was plain FTP; TLS was not tested in this run |
| Hearing the dashboard sound | The test browsers have no speakers; the number of chimes was counted instead |
| The arm64 image on a real ARM machine | Built and pushed for both platforms; only the amd64 image was run |

## Running it again

```bash
sh tests/e2e_run.sh
```

It needs Docker, the `cognex-monitor:test` image (`docker build -t cognex-monitor:test .`)
and the Playwright image (`mcr.microsoft.com/playwright/python:v1.48.0-jammy`).
It never touches the normal local stack or its data. `KEEP=1` leaves the test
stack running to look at it on http://localhost:8400.
