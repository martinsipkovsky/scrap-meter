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
the parts entered in `raw_ok` / `raw_nok`.

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

The bundled database is only reachable inside Docker. To let Power BI reach it
and create the read-only login:

1. Copy `deploy/docker-compose.powerbi.yml` next to the server's
   `docker-compose.yml`, renamed to **`docker-compose.override.yml`** (compose
   reads it automatically; the main compose file stays unchanged).
2. Add to the `.env` in the same folder:

   ```bash
   POWERBI_PASSWORD=<a strong password>
   POWERBI_DB_PORT=5432
   ```

3. `docker compose up -d`.

On startup the app creates the login `powerbi` (or `POWERBI_USER`) with that
password, or updates its password, and lets it read the `powerbi_*` views and
nothing else. The database is then published on `POWERBI_DB_PORT` of the
server.

**Firewall:** the database port is open on the server from then on. Allow it
only from the PC that runs Power BI Desktop or the gateway, for example:

```bash
sudo ufw allow from 192.168.1.50 to any port 5432 proto tcp
```

The bundled database has no TLS, so the connection is not encrypted; keep it
inside the plant network.

To turn it off again, delete `docker-compose.override.yml` and run
`docker compose up -d`; the port is closed and the login can no longer reach
the database from outside.

**Database on another server** (chosen on the Database tab): the app creates
the read-only login there too when `POWERBI_PASSWORD` reaches the app (the
override file passes it) and its database user may create roles; otherwise
the log says why and an administrator of that server creates it with:

```sql
CREATE ROLE powerbi LOGIN PASSWORD '…';
GRANT CONNECT ON DATABASE cognex TO powerbi;
GRANT USAGE ON SCHEMA public TO powerbi;
GRANT SELECT ON powerbi_station_comments, powerbi_stations, powerbi_job_totals,
                powerbi_readings, powerbi_devices TO powerbi;
```

## Power BI Desktop

1. Home → Get data → More… → **PostgreSQL database** → Connect.
2. Server: `<server IP>:5432` (the `POWERBI_DB_PORT`), Database: `cognex`
   (`POSTGRES_DB`). Pick **Import**, or DirectQuery for live data → OK.
3. Sign in on the **Database** tab of the sign-in window with `powerbi` and the
   password (an administrator can show it on the Database tab).
4. If Power BI says the connection isn't encrypted, click OK to connect
   without encryption (or in File → Options and settings → Data source
   settings → this source → Edit permissions, clear *Encrypt connections*).
5. Tick the views you need (comments: `powerbi_station_comments`) → Load.

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
