# Database, backups and Power BI

The Database tab is for administrators.

![Database tab](images/database.png)

- **Database**: which database server the app uses.
- **Backups**: **Download backup** saves everything in one file, **Import
  backup** puts one back, and the automatic backup sends one to an FTP server
  on a schedule. The tab warns when the last backup is more than 7 days old.
- **Recalculate counts from raw readings**: versions up to 1.25 counted a
  whole NOK counter again whenever only the OK counter of a device was reset.
  **Check what would change** shows the OK / NOK per station and day now and
  corrected; **Correct these counts** applies it after saving a backup on the
  server. Nothing changes before you confirm.
- **Reading the data**: Power BI, Excel or SQL can read the data through a
  read-only login and ready-made views (`powerbi_*`), including daily
  figures. Switch on **Power BI access** to open a port for it.

More: [Database tab and backups](../user-guide.md#database-tab-and-backups),
[Reports](../reporting.md).
