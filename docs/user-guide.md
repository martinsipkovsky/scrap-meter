# User guide

Scrap Meter has two building blocks:

- **Devices** are the connections the app reads: a Cognex camera, a PLC, an
  OPC UA server, a counter or gateway. A device only delivers values (for most
  protocols its pass, fail, total and job; for OPC UA any value of the server).
- **Stations** are what is counted, e.g. machine "M1". A station's OK, NOK,
  total (optional) and job (optional) each come from one device and one of its
  values, so a station can combine several devices: OK pieces from device 1,
  NOK pieces from device 2. Parts can also be entered by hand. The dashboard,
  statistics, alerts, chat commands and the OEE meter all work per station.

Up to version 1.4 every device was counted by itself. When 1.5 starts for the
first time it gives each existing device its own station with the same name
and the same history, so the dashboard looks as before.

## Dashboard

One block per station with its connection state, current job, and the running
OK/NOK totals and scrap rate for that job (since the last **Reset counters**,
if one was pressed on the station view). Stations that are not in production
(see below) are shown **grayed out**. Click a station to open its station
view.

Below the counters each block shows the station's latest comment (author,
time, scrap at that moment). **+ Comment** / **Comments** opens the station's
comments to read them or write a new one without leaving the dashboard. See
[Comments](#comments).

At the bottom, **Last 24 hours** shows the OEE meter and the total OK and NOK
of the stations included in the statistics. See [OEE](#oee).

## Devices tab

Add a device with:

| Field | Meaning |
|---|---|
| Name | Unique display name |
| Protocol | How the app talks to this device. See [Protocols](protocols.md). |
| Host / Port | For polled protocols: the device's or PLC's address. For listener protocols: *Port* is the port the app listens on, and *Host* optionally restricts which IP may send (blank = anyone). OPC UA uses the endpoint URL instead. |
| Config (JSON) | Protocol settings. The form lists the fields for the chosen protocol. OPC UA has its own form fields instead (see below). |
| Poll interval | Seconds between reads, for polled protocols |
| Also add a station | Makes a station with the same name that counts this device's pass and fail (and its total and job). Untick it when the device's values go into a station that combines devices. Not offered for OPC UA, whose stations pick nodes. |

The list shows each device's latest values and the stations that use it.
**Poll now** reads a polled device immediately (and updates its stations),
which is the quickest way to check a new configuration. A device that a
station uses can't be deleted until the station is changed or deleted.

### OPC UA devices

An OPC UA device is only the connection to the server:

- **Endpoint URL** of the server, e.g. `opc.tcp://10.0.0.5:4840`.
- **Security mode** None, Sign or SignAndEncrypt, and the **security policy**
  (Basic256Sha256 unless the server needs another one).
- **Login**: anonymous, or username and password. The password is never shown
  again or exported; leaving it as `********` keeps the saved one.
- **Test and browse the server…** connects with these settings and lists the
  server's address space.

The values are picked on the station: any variable the server exposes. With
Sign or SignAndEncrypt the app logs in with its own client certificate, made
on first use and kept in the data volume. If the server rejects it, download
it with the link in the form and add it to the server's trusted certificates.
See [Protocols](protocols.md#opc-ua-client-opcua).

## Stations tab

Add a station with:

| Field | Meaning |
|---|---|
| Name | Unique display name, e.g. `M1` |
| OK (pass) count | Device and value that count the good parts |
| NOK (fail) count | Device and value that count the bad parts |
| Total count (optional) | Device and value of a total counter; without it total = OK + NOK |
| Job (optional) | Device and value holding the job or recipe; without it the station uses *Job name when no job value is set* (default `MAIN`) |
| Order | Lower numbers come first on the dashboard |
| Production idle timeout | Minutes without an OK increase before the station counts as idle (default 30) |
| Ideal cycle time | Seconds per part at full speed, for the OEE performance factor (optional) |
| Statistics | **Include** (default) or **Exclude**, see [Stations excluded from the statistics](#stations-excluded-from-the-statistics) |

Pick OK, NOK or both; a missing one counts as 0. A station with no device at
all is fed only by [manual entries](#manual-entries). For most devices the
value is chosen from a list (OK counter, NOK counter, total counter, job name,
with the current value). For an OPC UA device, type a node id or press
**Browse…**: open folders and objects, then **Use** on the variable. Current
values and data types are shown to help pick the right node.

A station is **online** when every device it uses is online and has delivered
its value; otherwise the list and the dashboard say what is missing (e.g. "no
value 'ns=2;s=M1.Bad' from device 'PLC' yet").

Deleting a station deletes its counters, readings and alert rules; its devices
stay.

## How counting works

Whenever a device is read (or pushes a record), every station that uses it
takes the latest values of its devices: the raw OK, NOK and total counters and
the current job. The app adds the **increase** since the station's previous
reading to a running total for that station and job.

- **Counter reset on a device:** the app sees a counter drop, treats the new
  value as counted from zero, and keeps adding. When all of a station's
  counters come from one device, a drop in one of them means all were reset
  together. When they come from different devices, each counter is checked on
  its own, so resetting the reject counter does not touch the OK count.
  Banked totals are never lost, but parts counted between the last reading and
  the reset can't be seen, so poll often enough for your line speed.
- **Job change:** the old job's totals are frozen and a new running total
  starts. If an earlier job comes back, its totals continue where they stopped.
- **First reading:** the counters a device already shows when a station first
  sees them are the starting totals, not parts made since.

## Production state

A station is **running** while its OK counter keeps increasing. If it does not
increase for the station's idle timeout, the station becomes **idle**: it is
grayed out on the dashboard and scrap-rate and fail-count alerts are
suppressed, because NOK counts on a stopped line are usually false signals.
The next OK puts it back into production.

Disconnect alerts are still sent for idle stations.

## Station view

Opening a station shows its devices, an OK/NOK chart over 1 hour, 8 hours,
24 hours or 7 days, and these buttons:

- **Stop** puts the station out of production by hand. It stays stopped until
  someone presses Start, even if parts are counted.
- **Start** clears a manual stop and restarts the idle clock. A station that
  still doesn't count goes idle again after its timeout.
- **Reset counters** sets the OK / NOK counters shown on the dashboard and the
  station view for the current job back to zero, after a confirmation (needs
  the *control_connections* permission). The card then says "Since reset" with
  the time. Nothing is sent to the devices, and the job totals in the Data log,
  the chart, the readings history and the scrap statistics stay as they were.
  Scrap and fail-count alerts follow the reset counters.

With the `manual_entry` permission the station view also has an **Add entry**
form, see [Manual entries](#manual-entries). Every user who sees the
dashboard gets the **Comments** box, see [Comments](#comments).

The station view is at `/station/<id>`; links from earlier versions
(`/camera/<id>`, `/device/<id>`) still open it.

## Comments

Comments note what happened on a station: a tool change, a material batch, a
stop for cleaning. Write one in the **Comments** box on the station view, or
from the station's block on the dashboard, and press **Add comment** (or
Ctrl+Enter). Anyone who can see the dashboard can write comments.

Each comment is saved with:

- the time (stored in UTC, shown in your browser's time zone) and the author;
- the station's name and current job;
- the OK, NOK and scrap % shown on the station at that moment (the job's
  counters since the last **Reset counters**, as on the dashboard).

That snapshot never changes, even when the station is renamed or counts on.
Comments are listed newest first, and the latest one also shows on the
station's dashboard block. Only an administrator can delete a comment.
Comments are kept when their station is deleted, are part of backups, and
reports read them from the `powerbi_station_comments` view (see
[Reports](reporting.md)).

## Manual entries

Parts counted by hand, for example after a manual check or on a machine
without a connection, are added on the station view under **Add entry**: OK
and NOK parts, the time (now by default; set an earlier time to add parts made
earlier), the job (the current job by default) and an optional note. Needs the
`manual_entry` permission (administrators have it).

A manual entry counts like device data: it adds to the station's counters on
the dashboard and the station view, and its parts are in the OK/NOK chart,
Scrap statistics, the Excel export, chat command replies and the OEE meter's
quality and totals at the time of the entry. It adds no time in production.

In the Data log manual entries are marked **manual** with who entered them and
the note. They can be excluded and included like any reading, and **Edit**
(parts, time, job, note) or **Delete** changes them; the station's counters
follow.

## OEE

The bottom of the dashboard shows OEE over the last 24 hours:

- **Availability** = time in production / 24 h. There are no shifts or planned
  stops yet, so the whole 24 hours counts as planned time; a line that runs
  one shift a day can reach at most about 33 %.
- **Performance** = ideal cycle time × parts made / time in production. It
  needs the station's ideal cycle time; a value above 100 % means the ideal
  cycle time is set too long.
- **Quality** = OK / (OK + NOK).
- **OEE** = availability × performance × quality.

Time in production is the time between device readings while the station
was in production (a gap counts at most the idle timeout); manual entries add
parts but no time. Parts are counted like
Scrap statistics: excluded readings and parts made while not in production are
left out. The meter, the factors and the total OK / NOK cover the stations
included in the statistics; the OEE itself covers those of them with an ideal
cycle time, and the panel names the stations without one. **Per station**
under the meter shows the same figures for every station.

## Data log

The running totals per station and job, and the reading history, as stored in
the database. Each reading shows whether its parts count in the scrap
statistics:

- **counted**: included.
- **not in production**: taken while the station was idle or stopped, so left
  out.
- **excluded**: left out by a user.
- **station not in totals**: the station is excluded from the statistics by
  default.
- **included**: a reading of such a station that a user included.

Users with the `exclude_readings` permission get an **Exclude** button on each
counted reading, and **Include** on a left-out one. Choose **Excluded readings
only** to find them again.

## Scrap statistics

Pass, fail, total parts and scrap % for a date range. It is in the left panel
for anyone with the `view_data` permission.

- Pick **Current month** (the default), **Today**, **Last 7 days** or
  **Last 30 days**, or set your own **From** and **To** dates (both days
  included, up to a year).
- The figures are shown overall, per station, per job and per day. A line
  under the totals says how many parts were left out, and why.
- **Download Excel** saves an .xlsx file with the same figures as the screen,
  one sheet each for Overall, Per station, Per job and Per day.

Parts are counted from the reading history the same way as the station view's
OK/NOK chart: the growth of each job's totals between readings. Scrap is
fail / (pass + fail). Days are calendar days in the time zone of the browser
you are using. Parts a station had already counted before the app first saw a
job are not included, so the figures can be a little lower than the running
totals.

These parts are left out of the overall figures:

- **Not in production:** parts counted while the station was idle (no OK
  increase within its idle timeout) or stopped by an operator, the same state
  the dashboard grays out. For readings logged before this was recorded
  (before October 2026) only the idle rule is applied, using the station's
  current idle timeout.
- **Excluded readings:** readings a user excluded, one at a time in the Data
  log or for a whole period under **Exclude or include a time period** on this
  page. Pick a station (or all stations) and a start and end time, then press
  **Exclude**. **Include** with the same period takes them back. Excluding a
  reading drops only the parts counted since the reading before it.
- **Stations excluded from the statistics** (see below).

### Stations excluded from the statistics

A station whose **Statistics** setting is **Exclude**, for example a test rig
or a line still being set up, keeps its own rows in *Per station* and *Per
job* (marked **not in totals**, and "No (excluded by default)" in the Excel
file), but its parts are not in the overall figures, the per-day rows, the
totals of chat commands such as `!status`, or the OEE meter. Its parts are
listed under "Left out".

To count some of its parts anyway, include them like any excluded reading:
one reading at a time in the Data log, or a period with **Include** under
**Exclude or include a time period**. Those readings are then in the overall
figures; the badge's tooltip on the station row says how many parts that is.
Changing the setting applies to all readings of the station, old and new.

## Export and import

**Export** on the Devices tab downloads every device and station as one JSON
file (devices without passwords; stations name their devices). **Import**
reads such a file: devices and stations with a new name are added, existing
ones (same name) are updated, and a missing password keeps the saved one.
Counters and history are never touched. If anything in the file is invalid,
nothing is imported.

Files exported by earlier versions (Cognex Monitor, or Scrap Meter 1.4) import
too: each new device in them also gets its own station, with the idle timeout
and statistics setting from the file.

## Notifications tab

Alert rules (per station or for all stations), delivery providers, the linked
WhatsApp phone and the commands it answers in WhatsApp groups (e.g. `!status`).
See [Notifications](notifications.md).

## Users and permissions

Every page and API call requires a login. A user is either an
**administrator** (full access, including the Database tab) or a normal user
with any of these permissions. Users with `manage_users` create users and
assign permissions. The last administrator can't be deleted.

| Permission | Allows |
|---|---|
| `view_dashboard` | View the dashboard, stations, devices and the OEE meter; write comments on stations; export settings |
| `manage_devices` | Create, edit, delete and import devices and stations, browse OPC UA servers |
| `control_connections` | Poll devices, start/stop production, reset the dashboard counters |
| `view_data` | Browse logged readings and counters, scrap statistics |
| `exclude_readings` | Exclude readings from (or include them in) the scrap statistics |
| `manual_entry` | Add, edit and delete manual entries (OK / NOK parts counted by hand) |
| `manage_notifications` | Configure notification rules and providers |
| `manage_users` | Create users and edit their permissions |

The navigation only shows the tabs a user may open. The Database tab, and
deleting comments, are for administrators only.

Each user can change their own password under *Account*.

## Database tab and backups

Administrators see a *Database* tab. Besides choosing the database server, it
is where backups are made: **Download backup** saves all data in one file,
**Import backup** puts a backup file back (replacing the current data), and
the automatic backup sends a backup to an FTP server on a schedule. Take
backups regularly; the tab warns when the last one is more than 7 days old.
Backups made by earlier versions (Cognex Monitor, Scrap Meter 1.4) can be
imported; their devices become stations as on an update. See
[Configuration](configuration.md#backups-database-tab) for details.

The tab's **Reading the data** section is a short manual for reading all data
from the database with Power BI, Excel or SQL: connection details, the
read-only login, the views and example queries. See [Reports](reporting.md).
