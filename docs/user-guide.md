# User guide

## Dashboard

The dashboard shows every device with its connection state, current job, and
the running pass/fail totals and scrap rate for that job (since the last
**Reset counters**, if one was pressed on the device view).

Devices that are not in production (see below) are shown **grayed out**.
Devices excluded from the statistics say "not in statistics totals". Click a
device to open its device view.

## Devices tab

A device is anything the app reads counters from: a Cognex camera, a PLC, an
OPC UA server, a counter or gateway. Add one with:

| Field | Meaning |
|---|---|
| Name | Unique display name |
| Protocol | How the app talks to this device. See [Protocols](protocols.md). |
| Host / Port | For polled protocols: the device's or PLC's address. For listener protocols: *Port* is the port the app listens on, and *Host* optionally restricts which IP may send (blank = anyone). OPC UA uses the endpoint URL instead. |
| Config (JSON) | Protocol settings. The form lists the fields for the chosen protocol. OPC UA has its own form fields instead (see below). |
| Poll interval | Seconds between reads, for polled protocols |
| Idle timeout | Minutes without a new pass before the device counts as idle (default 30) |
| Statistics | **Include** (default) or **Exclude**: whether the device's readings count in the overall statistics. See [Devices excluded from the statistics](#devices-excluded-from-the-statistics). |

**Poll now** reads a polled device immediately, which is the quickest way to
check a new configuration.

### OPC UA devices

With the **OPC UA client** protocol the form shows:

- **Endpoint URL** of the server, e.g. `opc.tcp://10.0.0.5:4840`.
- **Security mode** None, Sign or SignAndEncrypt, and the **security policy**
  (Basic256Sha256 unless the server needs another one).
- **Login**: anonymous, or username and password. The password is never shown
  again or exported; leaving it as `********` keeps the saved one.
- **Node ids** for the pass, fail, total (optional) and job (optional) values.
  **Browse…** next to each field connects to the server with the settings
  above and lists its address space: open folders and objects, then press
  **Use** on a variable. Current values and data types are shown to help pick
  the right node.

With Sign or SignAndEncrypt the app logs in with its own client certificate,
made on first use and kept in the data volume. If the server rejects it,
download it with the link in the form and add it to the server's trusted
certificates. See [Protocols](protocols.md#opc-ua-client-opcua).

### Export and import

**Export** downloads every device's settings as one JSON file, including the
statistics setting but not passwords. **Import** reads such a file: devices
with a new name are added, devices whose name already exists are updated (a
missing password keeps the saved one). Counters and history are never
touched. If any device in the file is invalid, nothing is imported. Files
exported by earlier versions (Cognex Monitor) import as before; their devices
are set to Include.

## How counting works

Every reading gives the device's raw pass and fail counters and the current
job name. The app adds the **increase** since the previous reading to a running
total for that device and job.

- **Counter reset on the device:** the app sees a counter drop, treats the
  new values as counted from zero, and keeps adding. Banked totals are never
  lost, but parts counted between the last reading and the reset can't be
  seen, so poll often enough for your line speed.
- **Job change:** the old job's totals are frozen and a new running total
  starts. If an earlier job comes back, its totals continue where they stopped.

## Production state

A device is **running** while its pass counter keeps increasing. If the pass
counter does not increase for the device's idle timeout, the device becomes
**idle**: it is grayed out on the dashboard and scrap-rate and fail-count
alerts are suppressed, because NOK counts on a stopped line are usually false
signals. The next pass puts it back into production.

Disconnect alerts are still sent for idle devices.

## Device view

Opening a device shows an OK/NOK chart over 1 hour, 8 hours, 24 hours or
7 days, and these buttons:

- **Stop** puts the device out of production by hand. It stays stopped until
  someone presses Start, even if parts are counted.
- **Start** clears a manual stop and restarts the idle clock. A device that
  still doesn't count goes idle again after its timeout.
- **Reset counters** sets the OK / NOK counters shown on the dashboard and the
  device view for the current job back to zero, after a confirmation (needs
  the *control_connections* permission). The card then says "Since reset" with
  the time. Nothing is sent to the device, and the job totals in the Data log,
  the chart, the readings history and the scrap statistics stay as they were.
  Scrap and fail-count alerts follow the reset counters.

The device view is at `/device/<id>`; old `/camera/<id>` links still work.

## Data log

The running totals per device and job, and the raw reading history, as stored
in the database. Each reading shows whether its parts count in the scrap
statistics:

- **counted**: included.
- **not in production**: taken while the device was idle or stopped, so left
  out.
- **excluded**: left out by a user.
- **device not in totals**: the device is excluded from the statistics by
  default.
- **included**: a reading of such a device that a user included.

Users with the `exclude_readings` permission get an **Exclude** button on each
counted reading, and **Include** on a left-out one. Choose **Excluded readings
only** to find them again.

## Scrap statistics

Pass, fail, total parts and scrap % for a date range. It is in the left panel
for anyone with the `view_data` permission.

- Pick **Current month** (the default), **Today**, **Last 7 days** or
  **Last 30 days**, or set your own **From** and **To** dates (both days
  included, up to a year).
- The figures are shown overall, per device, per job and per day. A line under
  the totals says how many parts were left out, and why.
- **Download Excel** saves an .xlsx file with the same figures as the screen,
  one sheet each for Overall, Per device, Per job and Per day.

Parts are counted from the reading history the same way as the device view's
OK/NOK chart: the growth of each job's totals between readings. Scrap is
fail / (pass + fail). Days are calendar days in the time zone of the browser
you are using. Parts a device had already counted before the app first saw a
job are not included, so the figures can be a little lower than the running
totals.

These parts are left out of the overall figures:

- **Not in production:** parts counted while the device was idle (no pass
  increase within its idle timeout) or stopped by an operator, the same state
  the dashboard grays out. For readings logged before this was recorded
  (before October 2026) only the idle rule is applied, using the device's
  current idle timeout.
- **Excluded readings:** readings a user excluded, one at a time in the Data
  log or for a whole period under **Exclude or include a time period** on this
  page. Pick a device (or all devices) and a start and end time, then press
  **Exclude**. **Include** with the same period takes them back. Excluding a
  reading drops only the parts counted since the reading before it.
- **Devices excluded from the statistics** (see below).

### Devices excluded from the statistics

A device whose **Statistics** setting is **Exclude**, for example a test rig
or a line still being set up, keeps its own rows in *Per device* and *Per job*
(marked **not in totals**, and "No (excluded by default)" in the Excel file),
but its parts are not in the overall figures, the per-day rows, or the totals
of chat commands such as `!status`. Its parts are listed under "Left out".

To count some of its parts anyway, include them like any excluded reading:
one reading at a time in the Data log, or a period with **Include** under
**Exclude or include a time period**. Those readings are then in the overall
figures; the badge's tooltip on the device row says how many parts that is.
Changing the setting applies to all readings of the device, old and new.

## Notifications tab

Alert rules, delivery providers, the linked WhatsApp phone and the commands
it answers in WhatsApp groups (e.g. `!status`). See
[Notifications](notifications.md).

## Users and permissions

Every page and API call requires a login. A user is either an
**administrator** (full access, including the Database tab) or a normal user
with any of these permissions. Users with `manage_users` create users and
assign permissions. The last administrator can't be deleted.

| Permission | Allows |
|---|---|
| `view_dashboard` | View dashboards and device data, export device settings |
| `manage_devices` | Create, edit, delete and import devices, browse OPC UA servers |
| `control_connections` | Start/stop device connections, polling and production, reset the dashboard counters |
| `view_data` | Browse logged readings and counters, scrap statistics |
| `exclude_readings` | Exclude readings from (or include them in) the scrap statistics |
| `manage_notifications` | Configure notification rules and providers |
| `manage_users` | Create users and edit their permissions |

The navigation only shows the tabs a user may open. The Database tab is for
administrators only.

Each user can change their own password under *Account*.

## Database tab and backups

Administrators see a *Database* tab. Besides choosing the database server, it
is where backups are made: **Download backup** saves all data in one file,
**Import backup** puts a backup file back (replacing the current data), and
the automatic backup sends a backup to an FTP server on a schedule. Take
backups regularly; the tab warns when the last one is more than 7 days old.
Backups made by earlier versions (Cognex Monitor) can be imported. See
[Configuration](configuration.md#backups-database-tab) for details.
