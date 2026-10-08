# Reports: Power BI, Excel and SQL

Power BI, Excel or any SQL client can read the app's data straight from its
PostgreSQL database, with a read-only login. The *Database* tab has the same
manual under **Reading the data**, filled in with this server's connection
details.

## What reports read

Reports read the `powerbi_*` **views**, not the tables. The app makes them on
every start. They hold all counted data with the station names filled in,
keep their column names when the app is updated, and leave out passwords and
tokens (device logins, user passwords, notification settings).

| View | Holds |
|---|---|
| `powerbi_station_comments` | Comments written on the stations, with the job and OK / NOK / scrap at that moment |
| `powerbi_stations` | The stations and their settings; `ideal_cycle_s` is the one of the current job |
| `powerbi_jobs` | Every job the stations have counted, with its ideal cycle time |
| `powerbi_job_totals` | OK / NOK per station and job since counting started (device counts plus manual entries) |
| `powerbi_readings` | Every reading and manual entry (the history behind the chart and the statistics) |
| `powerbi_daily_stations` | Every day per station: OK / NOK, scrap, parts left out, production time, availability, performance, quality, OEE, comments |
| `powerbi_daily_jobs` | Every day per station and job: OK / NOK, scrap, production time, performance |
| `powerbi_daily_overall` | Every day, all stations in the statistics together: OK / NOK, scrap, availability, performance, quality, OEE |
| `powerbi_devices` | The devices (connections), without their login settings |

All times are UTC (the `…_utc` columns).

**`powerbi_station_comments`**: `comment_id`, `commented_at_utc`,
`station_id`, `station_name`, `job`, `ok_count`, `nok_count`, `scrap_pct`
(0-100), `comment`, `author`. The station name, job, counts and scrap are
those shown on the station when the comment was written and never change
afterwards. Comments stay when their station is deleted.

**`powerbi_readings`**: `reading_id`, `read_at_utc`, `station_id`,
`station_name`, `job`, `manual_entry`, `raw_ok`, `raw_nok`, `raw_count`,
`total_ok`, `total_nok`, `in_production`, `excluded`, `included`, `note`,
`entered_by`, `device_id`, `source_id`, `ok_added`, `nok_added`. A device
reading holds the counters of the device that was read (`raw_*`, its
`device_id`, and the station source `source_id`), the running totals of its
job (`total_*`), and since 1.8 the pieces it counted (`ok_added` /
`nok_added`, 0 while the station was not in production). For older readings
`ok_added` is empty and the parts made between two readings are the
difference of the totals (see the hourly query below). A manual entry holds
the parts entered in `raw_ok` / `raw_nok`, negative for a correction (parts
taken back, since 1.14); sums over the entries already subtract them. A day
taken below zero by a correction keeps its negative `ok_count` / `nok_count`
in the daily views, with `scrap_pct` and `quality_pct` held within 0 - 100.

### Daily data

The app fills in the `powerbi_daily_*` views itself: one row per station and
day, per station, job and day, and per day for all stations together. Today's
rows are updated every 5 minutes, a day's rows are final once it is over, and
the last 7 days are computed again each night (manual entries or readings
excluded later). After the update to 1.11 every past day is computed once in
the background. **Compute again** on the Database tab redoes every day.

Days are calendar days in the time zone shown on the Database tab (the one
of the FTP backup settings, or UTC, until **Use <your time zone>** is
clicked; changing it computes every day again). Parts are counted like Scrap
statistics, production time and the OEE figures like the dashboard's OEE
meter. A station set to *Exclude* from the statistics has its rows with
`in_statistics` false and stays out of `powerbi_daily_overall`. Rows of a
deleted station stay (with its name) until every day is computed again.

**`powerbi_daily_stations`**: `day`, `station_id`, `station_name`,
`ok_count`, `nok_count`, `total_count`, `scrap_pct`, `manual_ok`,
`manual_nok` (manual entries, already in `ok_count` / `nok_count`),
`excluded_ok`, `excluded_nok` (readings a user excluded),
`not_in_production_ok`, `not_in_production_nok` (counted while idle or
stopped), `production_min`, `day_min` (minutes of the day, up to now for
today), `availability_pct`, `performance_pct` (empty without ideal cycle
times), `quality_pct`, `oee_pct`, `jobs`, `readings`, `comments`,
`in_statistics`, `first_reading_at_utc`, `last_reading_at_utc`,
`day_complete` (false for today), `timezone`, `updated_at_utc`. All
percentages are 0-100.

**`powerbi_daily_jobs`**: `day`, `station_id`, `station_name`, `job`,
`ok_count`, `nok_count`, `total_count`, `scrap_pct`, `manual_ok`,
`manual_nok`, `production_min`, `ideal_cycle_s`, `performance_pct`,
`updated_at_utc`.

**`powerbi_daily_overall`**: `day`, `stations`, `ok_count`, `nok_count`,
`total_count`, `scrap_pct`, `production_min`, `availability_pct`,
`performance_pct`, `quality_pct`, `oee_pct`, `comments`.

The tables behind them (`daily_stations`, `daily_jobs`) are read-only on the
Raw data tab and are part of backups.

### The other views

**`powerbi_job_totals`**: `station_id`, `station_name`, `job`, `ok_count`,
`nok_count`, `manual_ok`, `manual_nok`, `is_current_job`, `started_at_utc`,
`updated_at_utc`, `ideal_cycle_s` (the job's ideal cycle time).

**`powerbi_jobs`**: `job`, `ideal_cycle_s` (seconds per piece; empty when not
set), `updated_at_utc`. Join it to `powerbi_readings` or `powerbi_job_totals`
on `job`.

**`powerbi_stations`**: `station_id`, `station_name`, `current_job`,
`default_job`, `statistics` (`include` / `exclude`), `ideal_cycle_s` (the
ideal cycle time of the current job; per job since 1.7),
`idle_timeout_min`, `stopped_by_operator`, `last_reading_at_utc`,
`created_at_utc`.

**`powerbi_devices`**: `device_id`, `device_name`, `protocol`, `host`, `port`,
`enabled`, `connected`, `last_error`, `last_read_at_utc`.

With the app's own database login (administrators) the tables behind the
views can be read too: `stations`, `devices`, `readings`, `counter_states`,
`jobs`, `station_comments`, `notification_logs`. Their columns may change between
versions; reports should use the views.

## Setting it up on the server

On the **Database** tab, under *Power BI access*, click **Turn on**. Nothing on
the server has to change:

- the app creates the read-only login `powerbi` with a generated password
  (**Show** on the Database tab; **New password** makes another one), and
- it opens one of the listener ports for it (`5119`, the last of
  `LISTEN_PORTS`, by default; pick another one before turning it on). The
  compose file already publishes those ports. The app passes connections on
  that port on to the database.

Power BI then connects to `<server IP>:5119`. It stays on after restarts and
image updates until **Turn off**, which closes the port and takes the login's
right to sign in away. While it is on, no device can use that port.

Only the read-only login gets through that port: any other user (such as the
app's own database login) is refused. The login may read the `powerbi_*`
views and nothing else.

**Firewall:** allow the port only from the PC that runs Power BI Desktop or the
gateway, for example:

```bash
sudo ufw allow from 192.168.1.50 to any port 5119 proto tcp
```

The connection is not encrypted (the bundled database has no TLS); keep it
inside the plant network.

### Older setup with docker-compose.powerbi.yml

Servers set up before 1.11 with `deploy/docker-compose.powerbi.yml` as
`docker-compose.override.yml` and `POWERBI_PASSWORD` / `POWERBI_DB_PORT` in
the `.env` keep working: that publishes the database itself on
`POWERBI_DB_PORT`, and the login gets `POWERBI_PASSWORD`. To move to the new
way, delete `docker-compose.override.yml` and the two lines in the `.env`, run
`docker compose up -d`, and turn Power BI access on on the Database tab (the
login gets a new password, and Power BI the new port).

**Database on another server** (chosen on the Database tab): Power BI can
connect to that server directly. The app creates the read-only login there
when its database user may create roles (*Turn on* says why when it can't);
otherwise an administrator of that server creates it with:

```sql
CREATE ROLE powerbi LOGIN PASSWORD '…';
GRANT CONNECT ON DATABASE cognex TO powerbi;
GRANT USAGE ON SCHEMA public TO powerbi;
GRANT SELECT ON powerbi_station_comments, powerbi_stations, powerbi_jobs,
                powerbi_job_totals, powerbi_readings, powerbi_daily_stations,
                powerbi_daily_jobs, powerbi_daily_overall, powerbi_devices TO powerbi;
```

## Power BI Desktop

1. Home → Get data → More… → **PostgreSQL database** → Connect.
2. Server: `<server IP>:5119` (the port shown on the Database tab), Database:
   `cognex` (`POSTGRES_DB`). Pick **Import**, or DirectQuery for live data → OK.
3. Sign in on the **Database** tab of the sign-in window with `powerbi` and the
   password (an administrator can show it on the Database tab).
4. If Power BI says the connection isn't encrypted, click OK to connect
   without encryption (or in File → Options and settings → Data source
   settings → this source → Edit permissions, clear *Encrypt connections*).
5. Tick the views you need (one row per station and day:
   `powerbi_daily_stations`; comments: `powerbi_station_comments`) → Load.

Join views on `station_id` (for example comments with job totals) in the
model view.

## Scheduled refresh (Power BI service)

The Power BI service in the cloud can't reach a plant server directly. Install
the **on-premises data gateway** (standard mode) on a Windows PC in the plant
network that reaches the server on the database port, sign it in with your
Power BI account, then in the Power BI service:

1. Settings → Manage connections and gateways → New → On-premises, choose the
   gateway, type **PostgreSQL**, the same server and database, Basic
   authentication with `powerbi` and its password.
2. Publish the report from Power BI Desktop, open the semantic model's
   settings, under *Gateway and cloud connections* map the data source to that
   connection, and set a refresh schedule.

Allow the database port in the server's firewall from the gateway PC.

## Excel

1. Data → Get Data → From Database → **From PostgreSQL Database**.
2. Server and database as above. Under Advanced options you can paste one of
   the SQL queries below.
3. Sign in on the Database tab with the read-only login, pick the views →
   Load. Data → Refresh All reads the latest data.

## Example SQL queries

OK / NOK, scrap and OEE per station and day, last 30 days:

```sql
SELECT day, station_name, ok_count, nok_count, scrap_pct, production_min, oee_pct
FROM powerbi_daily_stations
WHERE day > current_date - 30
ORDER BY day DESC, station_name;
```

Comments of the last 7 days:

```sql
SELECT commented_at_utc, station_name, job, ok_count, nok_count, scrap_pct, comment, author
FROM powerbi_station_comments
WHERE commented_at_utc > now() - interval '7 days'
ORDER BY commented_at_utc DESC;
```

Latest comment of each station:

```sql
SELECT DISTINCT ON (station_id) station_name, commented_at_utc, job, scrap_pct, comment, author
FROM powerbi_station_comments
ORDER BY station_id, commented_at_utc DESC;
```

OK / NOK and scrap per station and job:

```sql
SELECT station_name, job, ok_count, nok_count,
       ROUND(100.0 * nok_count / NULLIF(ok_count + nok_count, 0), 2) AS scrap_pct
FROM powerbi_job_totals
ORDER BY station_name, job;
```

Parts per station and hour (devices and manual entries):

```sql
SELECT station_name, date_trunc('hour', read_at_utc) AS hour_utc,
       SUM(ok) AS ok, SUM(nok) AS nok
FROM (
  SELECT station_name, read_at_utc,
         CASE WHEN manual_entry THEN raw_ok
              ELSE COALESCE(ok_added, total_ok - LAG(total_ok, 1, total_ok) OVER w) END AS ok,
         CASE WHEN manual_entry THEN raw_nok
              ELSE COALESCE(nok_added, total_nok - LAG(total_nok, 1, total_nok) OVER w) END AS nok
  FROM powerbi_readings
  WINDOW w AS (PARTITION BY station_id, job, manual_entry ORDER BY read_at_utc, reading_id)
) parts
GROUP BY station_name, hour_utc
ORDER BY hour_utc DESC, station_name;
```

This counts all parts (`ok_added` since 1.8, the difference of the totals
before); the Scrap statistics tab also leaves out excluded readings and, for
readings from before 1.8, time out of production.

Manual entries:

```sql
SELECT read_at_utc, station_name, job, raw_ok AS ok, raw_nok AS nok, note, entered_by
FROM powerbi_readings
WHERE manual_entry
ORDER BY read_at_utc DESC;
```

From the server itself, without opening a port, the same queries run with
`docker compose exec db psql -U cognex cognex`.
