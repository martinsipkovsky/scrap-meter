# User guide

Scrap Meter has two building blocks:

- **Devices** are the connections the app reads: a Cognex camera, a PLC, an
  OPC UA server, a counter or gateway. A device only delivers values (for most
  protocols its pass, fail, total and job; for OPC UA any value of the server).
- **Stations** are what is counted, e.g. machine "M1". A station has one or
  more **sources**: a source is one device and which of its values give the
  OK, NOK, total (optional) and job (optional). So a station can have two
  cameras, each with its own counters and job, or take OK pieces from device
  1 and NOK pieces from device 2. Parts can also be entered by hand. The
  dashboard, statistics, alerts, chat commands and the OEE meter all work per
  station.

Up to version 1.4 every device was counted by itself. When 1.5 starts for the
first time it gives each existing device its own station with the same name
and the same history, so the dashboard looks as before. From 1.5 to 1.7 a
station took each role (OK, NOK, total, job) from one device; 1.8 turns that
into one source per device, see [Upgrading to 1.8](#upgrading-to-18).

## The left menu

The **‹** button at the top of the menu hides it down to a strip of icons
(hover an icon for its name), and **›** brings it back. The browser remembers
the choice (until then the menu starts as set on the [Settings tab](#settings-tab)).
**Settings**, at the end of the menu, is there for every user. On a narrow screen, such as a phone, the menu is always the icon
strip; **›** opens it over the page, and a tap next to it closes it again.

The bottom of the menu shows the version the server runs (**Version 1.17.0**,
or **v1.17** in the icon strip). Clicking it opens the **Changelog**: every
release, newest first, with its date and what changed. Under it, **Tutorial** opens short
pages with pictures on everything the app can do ([docs/wiki](wiki/README.md)),
served by the app itself, so they also work without internet; they link to
this guide where it goes deeper.

## Dashboard

One block per station with its connection state, its devices, current job,
and the running OK/NOK totals and scrap rate of the jobs it runs now (since
the last **Reset counters**, if one was pressed on the station view). When
the sources run different jobs, the block shows them all (`A1 + PLC_JOB_1`)
and adds up their counters.

**Active** names the devices of the sources in production now. When none is,
the block shows the device that was active last, grayed, with the time it
stopped. Stations that are not in production (see below) are shown **grayed
out**. Click a station to open its station view.

The pill at the top right of a block shows the production state (**In
production**, **Not in production**, **Stopped**). While a source of the
station has a problem, the pill shows the problem's [code](#problem-codes)
instead: red for an error, amber while the station only waits for a device
or a first value. With several problems it shows the first (errors first)
and how many more (`E03 +1`). Hover the pill for the messages; the station
view shows the code with the full message.

### Problem codes

| Code | Meaning |
|---|---|
| W01 | Waiting for the device to connect or send its first data (a listener waits on its port, or the device was just added) |
| W02 | Waiting for the first value of a counter or job from the device |
| W03 | A source's device is switched off (disabled on the Devices tab) |
| E01 | A source's device was deleted |
| E02 | Cannot connect to the device: refused, unreachable or unknown address |
| E03 | The device did not answer in time (timeout) |
| E04 | The connection to the device was lost or closed |
| E05 | The device's settings are wrong or incomplete (e.g. no endpoint URL) |
| E06 | A value could not be read: wrong address, node or register |
| E07 | The listening port is taken by another program |
| E09 | Another error of the device; the message says which |

W codes are amber (nothing has failed), E codes red. The codes stay the
same in later versions.

Below the counters each block shows the station's **availability**,
**performance** and **quality** over the last 24 hours (the same figures and
formulas as the [OEE](#oee) bar) and the **cycle time** of its current job:
the actual one, measured over the same 24 hours (production time on the job /
its pieces), next to the one set on the [Jobs tab](#jobs-tab) ("set 12.0 s ·
per shot of 4"). When a shot of the job makes several pieces, both are per
shot; otherwise per piece. Hover a figure for how it is worked out.
Comments are written and read on the station view (see
[Comments](#comments)).

**Last 24 hours**, pinned to the bottom of the screen, shows the OEE meter and
the total OK and NOK of the stations included in the statistics, and stays in
view while the station blocks scroll (the last blocks always end above it).
**Per station** opens the figures of each station inside the bar. See
[OEE](#oee).

**Sound.** The dashboard plays a short chime when a station starts or leaves
production, or its devices go offline or come back. Stations whose alerts are
muted make no sound, and several changes in one refresh chime once. The
**Sound** switch at the top turns it off or on; each browser remembers its own
choice, and sound is on until someone turns it off. Browsers only allow sound
after the page has been clicked once: until then the switch shows *click the
page to allow*, and any click on the page clears it.

## Devices tab

Add a device with:

| Field | Meaning |
|---|---|
| Name | Unique display name |
| Protocol | How the app talks to this device. See [Protocols](protocols.md). |
| Host / Port | For polled protocols: the device's or PLC's address. For listener protocols: *Port* is the port the app listens on, and *Host* optionally restricts which IP may send (blank = anyone). OPC UA uses the endpoint URL instead. |
| Config (JSON) | Protocol settings. The form lists the fields for the chosen protocol. OPC UA has its own form fields instead (see below). |
| Poll interval | Seconds between reads, for polled protocols |
| Also add a station | Makes a station with the same name and one source: this device's pass and fail (and its total and job). Untick it when the device is a source of a station that combines devices. Not offered for OPC UA, whose stations pick nodes. |

The list shows each device's latest values and the stations that use it.
**Poll now** reads a polled device immediately (and updates its stations),
which is the quickest way to check a new configuration. A device that a
station uses can't be deleted until the station is changed or deleted.

### Pictures and pieces

When a camera takes several pictures of one real piece (both sides, several
cavities, a re-check), its OK / NOK counters count pictures, not pieces. The
**Pictures and pieces** column of the [Jobs tab](#jobs-tab) sets, per job, how
pictures make a piece (**Set rule**). From then on the dashboard, statistics, OEE, notifications,
chat commands and Power BI count real pieces. The Data log keeps the camera's
own picture counters next to the pieces counted (*Counted*). Past data isn't
changed. Without a rule every picture is a piece, as before.

| Setting | Meaning | Example |
|---|---|---|
| Pictures per piece | N pictures make one piece | 2: OK, NOK → NOK +1; OK, OK → OK +1 |
| A piece is OK when | *all its pictures are OK* (default), or *at least K pictures are OK* | 3 pictures, at least 2: NOK, OK, OK → OK +1; NOK, NOK, OK → NOK +1 |
| A NOK picture ends the piece at once | "Next cavity when NOK": as soon as the piece is NOK it is counted, and the next picture starts a new piece | 3 pictures: OK, NOK → NOK +1, then OK, OK, OK → OK +1 |
| Timer (seconds) | A piece that hasn't got all its pictures this long after its first one is judged anyway | 2 pictures, 10 s: OK, then nothing for 10 s → judged by the next setting |
| When pictures are missing | At the timer, a job change, a production stop or a new piece id: *the piece is NOK* (default), *judge only the pictures taken* (OK unless more NOK pictures than allowed), or *don't count the piece* | 2 pictures, only OK came: NOK +1 / OK +1 / nothing |
| Piece id value | A value the device sends with each picture (for example a piece or cavity number). Pictures with the same id are one piece, up to N; a new id ends the open piece | id 7: OK, OK; id 8: OK, NOK → OK +1, NOK +1 |

The dialog shows these examples for the rule being edited. A piece that is
still waiting for pictures is shown under the rule (*Open: …*) and on the
station view.

**Exact or estimated:** a listener in *event* mode (one record per picture),
or any device read after every picture, gives the pictures in order, so the
pieces are exact. When a polled counter rose by several pictures between two
reads, the order of the OK and NOK pictures is unknown: the app then assumes
the worst, every NOK picture spoiling a piece of its own (2 OK and 2 NOK
pictures with 2 per piece → NOK +2). Those counts are an estimate; poll as
often as the line makes pictures, or use event mode, for exact pieces.

The timer is checked whenever the device is read: on every poll for polled
devices, and with the next record for listeners (a piece judged by the timer
is counted when the next picture arrives). Stopping production drops an
unfinished piece; changing job ends it under the old job by the *missing*
setting. Piece rules are part of the device export and of backups.

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
| Sources | One or more devices, each with its own values (below). **+ Add source** adds one, **Remove** takes one away. |
| Order | Lower numbers come first on the dashboard |
| Job name when no job value is set | The job of a station whose sources have no job value (default `MAIN`) |
| Production idle timeout | Minutes without an OK increase on any source before the station leaves production (default 30) |
| Statistics | **Include** (default) or **Exclude**, see [Stations excluded from the statistics](#stations-excluded-from-the-statistics) |
| HMI windows | Web pages of the station's devices shown on the station view, see [HMI windows](#hmi-windows) |

Each **source** has:

| Field | Meaning |
|---|---|
| Device | The device it reads |
| OK count | The value that counts the good parts (blank: none) |
| NOK count | The value that counts the bad parts (blank: none) |
| Total (optional) | A total counter; without it total = OK + NOK |
| Job (optional) | The value holding the job or recipe. Without it the source counts under the job of the station's first source that has one, else under *Job name when no job value is set*. |
| Starts production at | *N* OK pieces within *M* seconds (default 2 within 60). See [Production state](#production-state). |

A source needs OK, NOK or both (a missing one counts as 0), or only a job, to
give the job to the station's other sources. A station with no source at all
is fed only by [manual entries](#manual-entries). For most devices each value
is chosen from a list (OK counter, NOK counter, total counter, job name, with
the current value). For an OPC UA device, type a node id or press
**Browse…**: open folders and objects, then **Use** on the variable. Current
values and data types are shown to help pick the right node.

Examples:

- *Two cameras on one station:* source 1 is camera 1 with its OK, NOK and job,
  source 2 is camera 2 with its own. Each camera counts under its own job,
  and the station shows their sum.
- *OK and NOK from different devices:* source 1 is the PLC with its OK count
  and job, source 2 is the reject counter with only its NOK count. The
  rejects count under the PLC's job.

Changing a source to read another device or other values starts its counting
again from the next read (a new baseline), so no jump is counted.

A station is **online** when every device it uses is online and has delivered
its values; otherwise the list and the dashboard say what is missing (e.g. "no
value 'ns=2;s=M1.Bad' from device 'PLC' yet").

Deleting a station deletes its counters, readings and alert rules; its devices
stay.

## Jobs tab

The **Jobs** tab (between Devices and Data log) holds every job's settings.
It lists every job the stations have counted, from a device's job value, a
station's default job or a manual entry; a job appears there by itself the
first time it is counted, with no settings yet. For each job the list shows
its cycle time, how pictures make pieces, the stations running it now, the
stations that have counted it, and when it was last counted. Everyone who
sees the dashboard sees the tab; users with `manage_devices` change it.

**+ Add job** adds a job before production starts, so its settings are ready
from the first piece. Enter the job name or number exactly as the device
reports it (the job value of the station's source), or a station's job name
for manual entries. A job no station has counted (added here and not run yet,
or its stations were deleted) can be removed with **Remove**.

### Cycle time

The cycle time is the OEE performance factor, set as **X seconds per shot**
(one machine cycle at full speed) and **Y pieces per shot** (for example the
cavities of an injection mould). One piece then ideally takes X / Y seconds,
shown under the fields: 12 s per shot of 4 pieces = 3 s / piece. For a machine
that makes one piece at a time, leave the pieces at 1. Type the values next to
the job and press **Save** (or Enter); an empty seconds field clears the cycle
time. Cycle times set before 1.15 (seconds per piece) became X seconds per
shot of 1 piece, so nothing changed until they are edited.

The pieces per shot only set the cycle time. How camera pictures make pieces
is the separate [pictures and pieces](#pictures-and-pieces) rule in the next
column (a 4-cavity mould whose camera takes one picture per cavity needs no
rule; one picture of all 4 needs one).

### Upgrades from 1.6

Up to version 1.6 the cycle time was set per station. When 1.7 starts on an
older database (or an older backup is imported), each station's cycle time is
copied to the jobs that station has run, where the job has none yet. When
several stations ran the same job with different cycle times, the one of the
station that ran it most recently is kept, and the app log says so (`upgrade:
stations had different cycle times for job ...`).

## How counting works

Whenever a device is read (or pushes a record), each station source that
reads it works out its **increase** since its previous read: the change of its
raw OK, NOK and total counters. While the station is in production, the
increase is added to the station's running total for the source's job. While
it is not, nothing is added (see [Production state](#production-state)).

- **Counter reset on a device:** the app sees a counter drop, treats the new
  value as counted from zero, and keeps adding. A drop in one of a source's
  counters means the device reset all of them. Each source is checked on its
  own, so resetting the reject counter of one device does not touch the OK
  count of another. Banked totals are never lost, but parts counted between
  the last read and the reset can't be seen, so poll often enough for your
  line speed.
- **Job change:** the jobs the sources run now are the station's current jobs;
  the other jobs' totals are frozen. If an earlier job comes back, its totals
  continue where they stopped.
- **First read:** a source's first read is its baseline: the counters a device
  already shows are not counted. (Listeners in *event* mode count each part
  themselves from zero, so their first part counts.)
- **Raw data are kept:** every read is logged as a reading, also while nothing
  is counted, with the pieces it counted (the Data log's *Counted* column).

## Production state

A station **goes into production** when one of its sources makes its start
count of OK pieces within its start time: 2 OK within 60 seconds by default,
set per source. The pieces that started production count, also the pieces the
station's other sources made within that time. The station stays **running**
while any source's OK count keeps increasing. A source is **active** while the
station is running and its OK count rose within the idle timeout (a source
without an OK value is active whenever the station is running).

If no source's OK count increases for the station's idle timeout, the
station becomes **idle**: it is grayed out on the dashboard, its counters stop
changing, and scrap-rate and fail-count alerts are suppressed, because NOK
counts on a stopped line are usually false signals. It goes back into
production when a source's start rule fires again.

**Stop production** on the station view wins over everything: nothing is
counted until **Start production** is pressed. Start counts from that moment
and restarts the idle clock.

Disconnect alerts are still sent for idle stations.

### Upgrading to 1.8

When 1.8 starts on an older database (or an older backup is imported), every
station's roles become sources, one per device: a station with OK, NOK and
job from one camera gets one source, a station with OK from device 1 and NOK
from device 2 gets two. These sources start production on **1** OK piece, as
stations did before, so nothing changes for them; raise the start rule on the
Stations tab if a single piece should not count as production. Each source
carries on from the raw counters the station read last, so no piece is lost
at the upgrade. What changes for every station: counters no longer grow while
the station is idle or stopped (before 1.8 they did, and only the statistics
left those parts out). Export files from 1.5 to 1.7 import the same way.

## Station view

Opening a station shows its devices and which of them are active, a
**Sources** list (each source's device, whether it is active, its job, its
values and its start rule), an OK/NOK chart over 1 hour, 8 hours, 24 hours or
7 days, and these buttons:

- **Stop** puts the station out of production by hand. It stays stopped, and
  counts nothing, until someone presses Start, even if devices keep counting.
- **Start** clears a manual stop and restarts the idle clock. A station that
  still doesn't count goes idle again after its timeout.
- **Reset counters** sets the OK / NOK counters shown on the dashboard and the
  station view for the jobs it runs now back to zero, after a confirmation (needs
  the *control_connections* permission). The card then says "Since reset" with
  the time. Nothing is sent to the devices, and the job totals in the Data log,
  the chart, the readings history and the scrap statistics stay as they were.
  Scrap and fail-count alerts follow the reset counters.
- **Scrap warnings on / off** (needs the *manage_devices* permission, like
  editing the station) mutes the station's alerts: no scrap rate, fail count,
  disconnect, production change or job change notifications are sent for it.
  Turned off here, they stay off until someone turns the switch on again.
  `!mute <station>` in a WhatsApp group does the same until the station's job
  changes, and `!unmute <station>` turns them on again (see
  [Notifications](notifications.md#commands-in-whatsapp-groups)). While the
  alerts are muted, the station view says who muted them, since when and
  until what, and the dashboard block shows 🔇. Every mute and unmute is
  listed in the Data log.

With the `manual_entry` permission the station view also has an **Add entry**
form, see [Manual entries](#manual-entries). Every user who sees the
dashboard gets the **Comments** box, see [Comments](#comments).

If the station has [HMI windows](#hmi-windows), they are shown below the
cards at the top.

The station view is at `/station/<id>`; links from earlier versions
(`/camera/<id>`, `/device/<id>`) still open it.

### HMI windows

A station can show the web pages of its devices on its station view: a Cognex
camera's WebHMI, a PLC's web server, a robot or press panel. Add them in the
station's settings (Stations tab, **Edit**) under **HMI windows**:

| Field | Meaning |
|---|---|
| Name | Shown above the window, e.g. `Camera 1` |
| Address | The page, e.g. `http://192.168.0.10/` or `https://192.168.0.10:8443/`. An address without `http://` gets it. |
| Height | Pixels (150 to 4000, blank: 600). The window can also be made taller by dragging its bottom-right corner. |

**↑ / ↓** change the order, **Remove** takes a window away. Anyone who can
edit stations edits the list; everyone who can see the station sees the
windows. On the station view each window has:

- **▾ / ▸** to collapse or expand it. A collapsed window doesn't load its page;
  the browser remembers the choice.
- **↻** to reload the page.
- **⛶** to show it full screen (Esc leaves). Where the browser doesn't allow
  full screen, the window fills the browser window instead.
- **↗** to open the page in a new tab.

The page is loaded by the viewer's browser straight from the device, so:

- **The computer showing the station view must reach the device's address.**
  A phone on the office Wi-Fi may see Scrap Meter but not the machine network.
- **The device must allow its page inside another page.** Many devices forbid
  it with an `X-Frame-Options` or `Content-Security-Policy: frame-ancestors`
  header; some Cognex models (e.g. the D900, until a firmware update) do. Scrap
  Meter asks the device for the page from the server, and if the device forbids
  it the window says so and offers **Open in a new tab** (and **Try here
  anyway**). If the server can't reach the address, the window says that too.
- **An `http://` device page can't be shown inside Scrap Meter opened over
  `https://`** (browsers block it). The window then offers a new tab.

Pages that sign in, use websockets (a live camera image) or keep their own
settings work as when opened directly, because the browser talks to the device
itself. HMI windows are part of the device export and of backups.

## Comments

Comments note what happened on a station: a tool change, a material batch, a
stop for cleaning. Write one in the **Comments** box on the station view and press **Add comment** (or
Ctrl+Enter). Since 1.15 the dashboard blocks no longer show comments. Anyone who can see the dashboard can write comments.

Each comment is saved with:

- the time (stored in UTC, shown in your browser's time zone) and the author;
- the station's name and current job;
- the OK, NOK and scrap % shown on the station at that moment (the job's
  counters since the last **Reset counters**, as on the dashboard).

That snapshot never changes, even when the station is renamed or counts on.
Comments are listed newest first. Only an administrator can delete a comment.
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

### Corrections (negative entries)

To take back parts that were counted by mistake (a double count, a test run,
a device that counted wrongly), enter a negative number of OK and/or NOK
parts, for example OK −10 and NOK 0. OK and NOK may each be positive,
negative or zero, but not both zero. A correction subtracts everywhere manual
entries count: the dashboard and station view counters, the OK/NOK chart,
Scrap statistics and the Excel export, the OEE meter, chat command replies
and the Power BI views (also the daily ones). Put it at the time (and job) of
the false parts so it lands on the right day.

- In the Data log a correction is marked **correction** and shows its parts
  with a minus sign. On the station view's chart a bar with a correction gets
  an orange marker, and its tooltip and the window's totals say how much was
  taken back.
- A correction never sends a scrap alert (also not when taking back OK parts
  raises the scrap rate); the next device reading or normal entry checks the
  alert rules as usual.
- If a correction would take the day's OK or NOK of the station or of the job
  below zero, the app says so and asks before saving it. Saved anyway, the
  dashboard shows 0 rather than a negative count, scrap and quality stay
  within 0 - 100 %, and the totals catch up as more parts are counted.

## OEE

The bottom of the dashboard shows OEE over the last 24 hours:

- **Availability** = time in production / 24 h. There are no shifts or planned
  stops yet, so the whole 24 hours counts as planned time; a line that runs
  one shift a day can reach at most about 33 %.
- **Performance** = ideal time of the parts made / time in production. The
  parts of each reading are weighed with the [ideal cycle time of their
  job](#cycle-time) (seconds per shot / pieces per shot), so a station that changes job during the
  day is weighed correctly. Parts of a job without a cycle time, and the time
  spent making them, are left out of performance, and the panel names those
  jobs. A value above 100 % means a cycle time is set too long.
- **Quality** = OK / (OK + NOK).
- **OEE** = availability × performance × quality.

Time in production is the time between device readings while the station
was in production (a gap counts at most the idle timeout); manual entries add
parts but no time. Parts are counted like
Scrap statistics: excluded readings and parts made while not in production are
left out. The meter, the factors and the total OK / NOK cover the stations
included in the statistics; the OEE itself covers those of them that made
parts of a job with a cycle time, or stand on such a job (an idle station on
it counts with availability 0). **Per station** under the meter shows the
same figures for every station, and which of its jobs have no cycle time;
each station's block on the dashboard shows them too, with its cycle time.

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

**Alerts muted and turned on** lists every time a station's alerts were
muted or turned on again: when, by whom, and from where (the station view's
switch, a WhatsApp command, or the job change that ended a `!mute`).

## Scrap statistics

How much scrap, where it comes from and how it changes, for a date range. It
is in the left panel for anyone with the `view_data` permission.

- Pick **Current month** (the default), **Today**, **Last 7 days** or
  **Last 30 days**, or set your own **From** and **To** dates (both days
  included, up to a year).
- **Tiles** at the top: scrap %, NOK pieces and pieces made, each with its
  change against the same number of days just before the range (scrap in
  percentage points, the counts in %; green is better, red is worse), and how
  many days with production were above the scrap alert.
- **Scrap per day** (per hour for Today or a range of two days): scrap % as a
  line, with the scrap alert as a dashed red line and the points above it in
  red. Days or hours without production are gaps. Hover for the figures.
- **Stations with the most NOK** and **Jobs with the most NOK**: the top 8 by
  NOK pieces, with their share of all NOK, their scrap % and its change.
- **Scrap per station and day**: one square per station and day, darker red
  for more scrap; a square with **!** was above that station's scrap alert.
  Hover a square for its figures. Shown for ranges of more than one day.
- **Per station**, **Per job** and **Per day** tables with OK, NOK, total and
  scrap, the share of all NOK and the change in scrap. Days above the scrap
  alert are marked. A line under the tiles says how many pieces were left out,
  and why.
- **Download Excel** saves an .xlsx file with the same figures as the tables,
  one sheet each for Overall, Per station, Per job and Per day (with an
  *Above scrap alert* column).

The **scrap alert** is the threshold of the enabled *Scrap rate* alert rules
on the Notifications tab: for the overall figures and days the lowest rule
for all stations, for a station its own lowest one (including its override on
a rule for all stations). Without such a rule, 5% is used, marked *(default)*.

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
file (devices without passwords; stations list their sources by device name), with every job
and its settings (cycle time per shot, pieces per shot, pictures-and-pieces
rule). **Import** reads such a file: devices and
stations with a new name are added, existing ones (same name) are updated, a
missing password keeps the saved one, jobs in the file with a cycle time
get it, and jobs not in the list yet are added. A file from 1.14 or older has
seconds per piece, which become seconds per shot of 1 piece. Stations bring their HMI windows; a file from 1.12 or older has none
and keeps the ones a station already has. Counters and history are never
touched. If anything in the file is invalid, nothing is imported.

Files from 1.5 and 1.6 hold a cycle time per station instead: it is given to
the jobs that station has run and to its default job, where they have none.

Files exported by earlier versions (Cognex Monitor, or Scrap Meter 1.4) import
too: each new device in them also gets its own station, with the idle timeout
and statistics setting from the file.

## Chat room

One messenger chat that everyone can read and write in from the web: a
WhatsApp or Signal group the linked phone is in, a Telegram chat of a
Telegram provider's bot, or a Discord channel of a Discord bot provider. It is in the left menu for users with the `chat_room`
permission (every user has it unless an administrator takes it away).

- An **administrator** picks the chat with **Choose the chat room** (later
  **Change room**). The list shows the WhatsApp and Signal groups of the
  linked phones, the chat ids of each Telegram provider and the channels of
  each Discord bot. Without any of them the tab says so and points to the
  Notifications tab.
  Changing the room keeps the old room's messages; **No chat room** removes it.
- The tab shows the messages written in the chat, what users sent from the
  web, and what the app sent there itself (alerts and command replies, marked
  *Scrap Meter*). New messages appear on their own; **Show older messages**
  loads the history, which is kept in the database.
- A message from the web goes out with the user's name in front: on WhatsApp
  as "*Martin:* text" from the linked phone, on Discord as "**Martin:** text"
  from the bot, on Telegram and Signal as "Martin: text".
  Discord and Signal messages reach the tab within a few seconds. Enter sends, Shift+Enter starts a new line. A message that could not
  be sent stays in the list, marked in red with the reason.
- In a WhatsApp, Discord or Signal room, chat commands work from the web too:
  `!status` written here is answered in the chat, like one typed there. Commands written in the
  group are answered as before.
- Photos, videos and files without a caption show as "[photo]", "[video]" or
  "[file]".
- A Telegram bot only sees every message of a group when its privacy mode is
  off (@BotFather, `/setprivacy`) or it is an admin of the group; otherwise
  only commands and replies to the bot reach the room.

## Notifications tab

Alert rules (per station or for all stations), delivery providers (WhatsApp,
Telegram, Discord, Signal, webhooks), the linked WhatsApp and Signal phones,
and the chat commands answered in WhatsApp and Signal groups and Discord
channels (e.g. `!status`).
See [Notifications](notifications.md).

## Users and permissions

Every page and API call requires a login. A user is either an
**administrator** (full access, including the Database tab) or a normal user
with any of these permissions. Users with `manage_users` create users and
assign permissions. The last administrator can't be deleted.

| Permission | Allows |
|---|---|
| `view_dashboard` | View the dashboard, stations, devices and the OEE meter; write comments on stations; export settings |
| `manage_devices` | Create, edit, delete and import devices and stations, add jobs and set their cycle times and piece rules (Jobs tab), browse OPC UA servers |
| `control_connections` | Poll devices, start/stop production, reset the dashboard counters |
| `view_data` | Browse logged readings and counters, scrap statistics |
| `exclude_readings` | Exclude readings from (or include them in) the scrap statistics |
| `manual_entry` | Add, edit and delete manual entries (OK / NOK parts counted by hand) |
| `manage_notifications` | Configure notification rules and providers |
| `chat_room` | Read and write in the Chat room (given to every user by default) |
| `manage_users` | Create users and edit their permissions |

The navigation only shows the tabs a user may open. The Database tab,
choosing the chat room, and deleting comments are for administrators only.

`chat_room` is ticked for every new user, and an update to 1.16 gives it to
every existing user once; untick it to take the Chat room away.

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

## Settings tab

Every user can set how the pages look and behave for them. The choices are
saved with the user, so they follow them to every browser they sign in on.

| Setting | Choices (built-in default first) |
|---|---|
| Mode | Dark, Light, As the system (follows the computer's or phone's light / dark setting) |
| Colour scheme | Blue, Green, Purple, Orange, Teal: buttons, links and the selected menu item |
| Density | Comfortable, Compact (less space around cards and table rows) |
| Text size | Normal, Small, Large (the whole page) |
| Date format | DD.MM.YYYY, YYYY-MM-DD, MM/DD/YYYY, DD/MM/YYYY |
| Time format | 24 hours (14:05), 12 hours (2:05 PM) |
| Start page | The page shown after signing in: Dashboard, Stations, Devices, Jobs, Data log, Scrap statistics or Chat room (one the user can open) |
| Dashboard refresh | Every 5 s, or 2 s, 10 s, 30 s, a minute |
| Dashboard sound | On, Off: what a browser starts with; the dashboard's Sound switch is then remembered per browser |
| Menu | Open, Folded: what a browser starts with; the menu button is then remembered per browser |

All settings are in one table: the **Yours** column holds the user's own
choice, and a setting left at **Default** follows the default for everyone.
Administrators see a second column, **Default for everyone**; a default left
at **Built-in** uses the built-in one above. **Reset to default** sets all of
the user's own choices back to Default. Mode, colour scheme, density and text
size change the page at once; dates, times and the dashboard settings apply
on the next page.

### Developer options

Administrators also see **Developer options**: unofficial ways into
messengers, off unless someone turns them on.

| Option | What it does |
|---|---|
| WhatsApp virtual client (QR login) | The app logs in as a linked device of a phone (the *WhatsApp phone* card on the Notifications tab) and sends from its number; its groups can get alerts, answer chat commands and be the Chat room. WhatsApp does not allow unofficial clients. |
| Signal (signal-cli) | Signal through the signal-cli service next to the app (see [Notifications](notifications.md#signal)): the *Signal phone* card, the Signal provider, commands and the Chat room in Signal groups. |

Turning an option off stops it and hides it: its card on the Notifications
tab, its groups in the Chat room and the command lists, and its provider
kind. Its providers stop sending (the alert log says the option is off).
Nothing is deleted: the linked phone stays linked and the history stays, so
turning it on again carries on where it was. A server updated from before
1.21 has an option on when it already used it (a linked WhatsApp phone,
WhatsApp providers or Chat room on it; Signal set up), so its alerts keep
going out; a new install starts with both off.

## Raw data tab

Administrators also see a *Raw data* tab: every table of the app, to look at
and fix single values without a database tool. The banner at the top has a
**Download backup first** button; changes go straight into the database, so
take one before larger fixes.

- **Tables:** pick a table on the left. Rows come page by page (25 to 200),
  newest first. Click a column header to sort, type in **Search** to find a
  text in any column (JSON settings aside), and add **Filters** (=, ≠, <, >,
  contains, starts with, is empty, ...).
- **Editing:** click a value to change it. The input fits the column: numbers,
  date and time (in UTC), true / false, a list of rows for a column that
  points to another table (e.g. a reading's station), JSON for settings.
  **✓** or Enter saves, **✕** or Esc cancels, **∅** empties a value that may be
  empty. A value of the wrong type is refused with the reason.
- **+ Add row** opens a form with an input per column; empty inputs get the
  column's default. **Delete** removes a row after a confirmation. Users and
  notification providers are added on their own tabs.
- **Change log:** every change made here, newest first, with who made it,
  when, the table, the row, and the values before and after. **Undo** puts the
  old values back, removes an added row, or puts a deleted row back with its
  id; the undo is logged too. A change can't be undone when the same values
  were changed again since (undo the later change first).
- **SQL (read only):** one SELECT query (or WITH ... SELECT), run in a
  read-only transaction; at most 500 rows are shown. Use it to look things up;
  changes belong in the grid, where they are logged.

Secrets stay hidden: password hashes and the notification providers' settings
(tokens) are shown as *hidden* and can't be edited, and passwords inside
settings (an OPC UA login) are masked; saving a masked password keeps the
stored one. The SQL box refuses those tables and columns and the backup and
database settings, as well as server functions that reach outside the data
(reading files, sleeping, and so on). The change log is read-only.
