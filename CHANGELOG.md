# Changelog

What changed in each release of Scrap Meter, newest first. The app shows this
file on its Changelog page (the version number at the bottom of the left menu).
Releases before 1.2.0 had no version number; they are listed as 1.0 and 1.1.

## 1.22.0 — 2026-10-08
- New **Tutorial** under the version number in the left menu: short pages with pictures on everything the app can do, from getting started to error codes. They are part of the app, so they work without internet, and link to the longer guides.

## 1.21.0 — 2026-10-08
- Settings has one compact table: your setting next to the default for everyone (administrators), and **Reset to default**.
- New **Developer options** for administrators: the WhatsApp virtual client (QR login) and Signal (signal-cli) are off unless turned on. Turning one off stops and hides it without deleting the linked phone or the history. A server that already used them keeps them on after the update.

## 1.20.0 — 2026-10-08
- Discord as a provider: a bot sends alerts into channels and reads them, so `!status`, `!mute` and the other chat commands work there and a channel can be the Chat room. A webhook mode only sends.
- Signal as a provider, through signal-cli in its own optional container (`deploy/docker-compose.signal.yml`): link a phone with a QR code on the Notifications tab, then send alerts to its groups, answer chat commands there and use a group as the Chat room.
- The command section is now "Chat commands" and lists WhatsApp groups, Discord channels and Signal groups to answer in.

## 1.19.0 — 2026-10-08
- New Settings tab at the end of the menu: dark, light or system mode, five colour schemes, compact density, text size, date and time format, the start page after signing in, the dashboard's refresh and sound, and a folded menu. Each user's choices follow them to every browser; an administrator sets the defaults for everyone.
- Dashboard blocks no longer show error messages. While a station has a problem, its pill shows a short code instead of the production state, red for an error and amber while it waits (`W01` waiting for the device, `E03` timeout, ...); hover it for the message. The station view shows the code with the full message, and the user guide lists every code.

## 1.18.1 — 2026-10-08
- Faster: Scrap statistics open about four to five times faster on a large database, and the dashboard's OEE bar, refreshed every 5 seconds, costs a fifth of what it did or less. The station view's chart loads faster over long periods. Nothing on the screens changes.
- The database gets an index on each station's readings by time; it is added once at the first start of this version.

## 1.18.0 — 2026-10-08
- The dashboard plays a chime when a station starts or leaves production, or its devices go offline or come back. Muted stations stay quiet, and several changes at once chime once.
- A Sound switch at the top of the dashboard turns it off or on; each browser remembers its choice (on by default). Until the page has been clicked once the switch says so, because browsers block sound before that.
- The explanation under the dashboard heading is gone; it is in the user guide.

## 1.17.0 — 2026-10-08
- Alerts of a station can be muted: `!mute Line 1` in a WhatsApp group mutes them until the station's job changes, `!unmute Line 1` turns them back on.
- The station view has a Scrap warnings switch next to Reset counters; turned off there, the station's alerts stay off until someone turns them on again.
- The station view says who muted the alerts and since when, the dashboard block shows a muted icon, and every mute and unmute is listed in the Data log.
- The version number is shown at the bottom of the left menu and opens this changelog.

## 1.16.0 — 2026-10-08
- New Chat room tab: one WhatsApp group or Telegram chat that everyone can read and write in from the web, with their name in front. An administrator picks the chat; the history is kept.
- Chat commands such as `!status` also work when written in the Chat room.
- Scrap statistics show more: scrap, NOK and pieces compared with the period before, a scrap trend against the scrap alert, the stations and jobs with the most NOK, and a station by day map of the days above the alert.

## 1.15.0 — 2026-10-08
- New Jobs tab with every job's settings in one place; jobs can be added before production starts.
- Cycle times are set as seconds per shot of a number of pieces, for multi-cavity moulds.
- Each dashboard block shows the station's availability, performance and quality and its actual against set cycle time.

## 1.14.0 — 2026-10-08
- Manual entries can be negative: a correction takes back pieces that were counted by mistake, everywhere the counts are used.

## 1.13.0 — 2026-10-08
- A station view can show the web pages of its devices (a Cognex WebHMI, a PLC web server), with reload, full screen and open in a new tab.

## 1.12.0 — 2026-10-07
- Pictures and pieces: per job, how many camera pictures make one real piece and when it is OK, so all figures count real pieces.

## 1.11.0 — 2026-10-07
- Power BI access is switched on from the Database tab, with no change on the server.
- The app keeps daily figures per station and job (OK, NOK, scrap, production time, OEE) for reports.

## 1.10.0 — 2026-10-07
- WhatsApp commands can list only the stations that were in production in the last few days.

## 1.9.0 — 2026-10-06
- The left menu can be hidden down to a strip of icons.
- The dashboard's last 24 hours bar stays at the bottom of the screen.
- New Raw data tab for administrators: every table, with editing, a change log with undo and a read-only SQL box.

## 1.8.0 — 2026-10-06
- A station can count several sources, for example two cameras each running their own job.
- A source starts production after a few OK pieces in a short time, so stray signals no longer start it.

## 1.7.0 — 2026-10-06
- Ideal cycle times are set per job, so the OEE weighs a station that changes job correctly.

## 1.6.0 — 2026-10-06
- Comments on stations, saved with the job and the counts at that moment.
- Read-only database views for Power BI and Excel, with a manual on the Database tab.

## 1.5.0 — 2026-10-06
- Stations: what is counted, made of the values of one or more devices.
- OEE meter for the last 24 hours on the dashboard.
- Manual entries of OK and NOK pieces counted by hand.

## 1.4.0 — 2026-10-06
- The app is now called Scrap Meter.
- OPC UA client for any OPC UA server, with a browse helper to pick values.
- A device can be left out of the scrap statistics by default, for example a test rig.

## 1.3.0 — 2026-10-02
- WhatsApp group commands: `!status` answers with every station's state, OK, NOK and scrap.

## 1.2.0 — 2026-10-02
- Reset counters on the camera view; history and statistics keep the full totals.
- WhatsApp alerts sent from your own number through a linked phone, and Telegram alerts.
- Alert rules for production and job changes, backups and app updates, with a level and their own destinations.
- Updates keep all settings.

## 1.1 — 2026-10-02
- Scrap statistics for a date range, overall, per camera, per job and per day, with an Excel download.
- Readings can be left out of the statistics, and time out of production is left out by itself.
- Backups: download, import, and automatic backups to an FTP server.

## 1.0 — 2026-10-01
- First release as Cognex Monitor: camera pass and fail counters with totals that survive counter resets, production state, alerts and users with permissions.
