# Getting started

Two things are set up once, then the app runs by itself:

1. **A device** for each machine or camera the app reads: a Cognex camera, a
   PLC (Modbus, SLMP, PROFINET gateway), an OPC UA server, or a camera that
   sends its results to the app. See [Devices and protocols](devices.md).
2. **A station** for each thing you want to count, e.g. a moulding machine.
   A station takes its OK, NOK and job from one or more devices. See
   [Stations](stations.md).

From then on the [Dashboard](dashboard.md) shows every station live, the
[Data log](data-log.md) keeps every reading, and
[Scrap statistics](scrap-statistics.md) add them up per day, station and job.

**Signing in.** Every user has a login. An administrator creates users and
decides what each one may do ([Users and permissions](users.md)).

**The left menu** lists the tabs you may open. The ‹ button folds it to icons;
the bottom shows who is signed in, the version (opens the changelog) and this
Tutorial.

![The dashboard with six stations](images/dashboard.png)

**Where to go next:** set up how the app looks for you on
[Settings](settings.md), and ask an administrator to set up alerts on
[Notifications](notifications.md).
