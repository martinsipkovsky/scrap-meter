# Known issues and limitations

## Not tested on real hardware

Every protocol (Data Channel, Modbus/TCP, Native Mode, TCP and UDP listeners,
SLMP client, SLMP server, PROFINET gateway, OPC UA client) has only been tested
against simulators and automated tests. **None has been run against a real
Cognex In-Sight camera, a real Mitsubishi PLC or a production OPC UA server.**
Field layouts, register widths, word order and SLMP framing in particular may
need adjusting; for OPC UA, the server's certificate trust and user settings
are the usual first hurdle. Reports from real installations are very welcome.

## Data Channel event mode drops consecutive passes

With the `datachannel` protocol in `mode: "event"`, each poll reads one record
and reports it as raw pass = 1 or fail = 1. The counter logic only counts
*increases* in raw values, so two passes in a row look like "1 then 1" and the
second one is not counted (the same applies to consecutive fails). Parts the
camera sends between polls are also missed, because only one record is read
per poll.

**Workaround:** use `counter` mode (have the camera send its running totals),
or use the **TCP listener** or **UDP listener** in event mode, which count every
record the camera pushes.

## PROFINET is gateway mode only

Native PROFINET IO is not supported and can't be from a user-space Python app.
The `profinet` driver reads counters through a PROFINET-to-Modbus/TCP gateway
or a PLC that exposes the data over Modbus/TCP. See
[Protocols](protocols.md#profinet-profinet-gateway-mode-only).

## WhatsApp linked phone is unofficial

Meta's official API can't post to WhatsApp groups. The `linked` WhatsApp
transport sends from your own number by logging in as a linked device, which
WhatsApp does not allow: it can restrict or ban numbers that use unofficial
clients, and a WhatsApp change can break it until the library is updated.
Use a spare number, and consider Telegram for alerts that must arrive. See
[Notifications](notifications.md).

## Counts lost across a counter reset

If a device's counters are reset between two readings, parts counted after the
last reading and before the reset can't be seen. Running totals are never
reduced. A shorter poll interval, or a push protocol, narrows the gap.

## OEE assumes 24 hours of planned time

There are no shifts, breaks or planned stops yet, so the OEE meter counts the
whole last 24 hours as planned production time. A line that runs one shift
shows an availability of about a third at best. Time in production comes from
device readings; manual entries add parts but no time.

## Single app instance

The poller and the listeners run inside the web process. Run one app
container per database; several replicas would poll each device several times
and fight over listener ports.
