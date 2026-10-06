# Protocols

> **None of these protocols has been tested on real Cognex cameras,
> Mitsubishi PLCs or OPC UA servers yet.** They are built from the vendors' protocol
> descriptions and tested against simulators and unit tests. Expect to adjust
> settings on first contact with real hardware.

Each protocol is one self-contained file in `app/protocols/`, so a protocol can
be read and debugged on its own. The Devices tab lists every protocol's config
fields, and each file's docstring describes them too.

There are two kinds:

- **Polled:** the app connects to the device (camera, PLC, OPC UA server)
  every *poll interval* seconds and reads the counters.
- **Push (listener):** the app opens a port and the device connects or sends to
  it. *Port* must be in `LISTEN_PORTS` (default 5100-5119), each push device
  needs its own port, and *Host* optionally limits which IP may send.

| Key | Name | Kind | File |
|---|---|---|---|
| `datachannel` | Cognex Data Channel (TCP) | Polled | `datachannel.py` |
| `modbus` | Modbus/TCP | Polled | `modbus.py` |
| `opcua` | OPC UA client | Polled | `opcua.py` |
| `tcp` | Generic TCP / Native Mode | Polled | `tcp.py` |
| `tcp_listen` | TCP listener | Push | `tcp_listener.py` |
| `udp_listen` | UDP listener | Push | `udp_listener.py` |
| `slmp` | SLMP / MC protocol (read PLC registers) | Polled | `slmp.py` |
| `slmp_listen` | SLMP server (device writes to the app as if it were a PLC) | Push | `slmp_server.py` |
| `profinet` | PROFINET (via gateway) | Polled | `profinet.py` |
| `simulator` | Simulated device | Polled | `simulator.py` |

## Counter mode and event mode

Text-based protocols support two record layouts:

- **`counter`**: each record carries the camera's running totals, e.g.
  `JOB_A,1520,12` (job, pass, fail).
- **`event`**: one record per inspected part, e.g. `JOB_A,Pass`. The app
  counts the parts itself.

For per-part results, prefer the **TCP or UDP listener** in event mode. The
polled Data Channel driver's event mode loses parts (see
[Known issues](known-issues.md)).

## Cognex Data Channel (`datachannel`)

The app connects to the camera's Data Channel / TCP port and reads one ASCII
record per poll.

| Field | Meaning |
|---|---|
| `delimiter` | Field separator (default `,`) |
| `terminator` | Record terminator (default CRLF) |
| `job_field` | Zero-based index of the job name |
| `pass_field` / `fail_field` | Index of the pass / fail counter (default 1 / 2) |
| `count_field` | Optional index of a total counter |
| `mode` | `counter` or `event` |
| `status_field` | Event mode: index of the Pass/Fail status (default 1) |
| `read_timeout` | Socket read timeout in seconds (default 5) |

## Modbus/TCP (`modbus`)

Reads counters from holding registers, e.g. a camera or PLC exposing results
over Modbus. Uses `pymodbus`.

| Field | Meaning |
|---|---|
| `unit_id` | Modbus unit/slave id (default 1) |
| `pass_register` / `fail_register` | Holding-register address of each counter |
| `count_register` | Optional total counter |
| `reg_width` | `1` for 16-bit, `2` for 32-bit (two registers) |
| `word_order` | `big` or `little` for 32-bit values |
| `job_registers` | Optional `[start, end]` registers holding the job name as ASCII |
| `job_string` | Fixed job name if the camera runs a single job |
| `timeout` | Connection timeout in seconds |

## Generic TCP / Native Mode (`tcp`)

Logs in with Cognex Native Mode (optional) and sends commands such as `GV…`,
or does plain send/receive, then extracts numbers with regular expressions.

| Field | Meaning |
|---|---|
| `login` / `password` | Native Mode credentials (optional) |
| `requests` | Map of `job` / `pass` / `fail` to the command string to send |
| `pattern_pass` / `pattern_fail` / `pattern_job` | Regex with one group that extracts each value |
| `timeout` | Socket timeout in seconds |

## TCP listener (`tcp_listen`)

The app listens on *Port*. Configure the camera as a TCP **client** pointing at
the server's IP and that port, sending one line per inspection.

| Field | Meaning |
|---|---|
| `delimiter` | Field separator (default `,`) |
| `terminator` | Record terminator (default CRLF; a bare LF also works) |
| `mode` | `counter` or `event` |
| `job_field` | Index of the job name, or `null` to always use `default_job` |
| `default_job` | Job name when the record has none (default `MAIN`) |
| `pass_field` / `fail_field` / `count_field` | Counter mode: field indexes (defaults 1 / 2 / none) |
| `status_field` | Event mode: index of the Pass/Fail status (default 1) |
| `pass_values` | Event mode: status values meaning pass (default `pass`, `ok`, `1`, `good`, `p`) |

## UDP listener (`udp_listen`)

Same fields as the TCP listener, but the camera sends each record as a UDP
datagram to the server's IP and *Port*. A datagram without a terminator is one
record.

UDP has no connection, so the camera shows online from its first datagram.
Extra field:

| Field | Meaning |
|---|---|
| `offline_after` | Seconds without a datagram before the camera shows offline. Default `0` = never, because a stopped line is not a fault. |

## SLMP / MC protocol client (`slmp`)

Cognex In-Sight cameras speak SLMP as the client: they connect to a Mitsubishi
PLC and write results into its registers. With this protocol, the camera
writes to the PLC as usual and **the app polls the PLC** using the SLMP 3E
binary frame. *Host/Port* are the PLC's IP and SLMP port (set in the PLC's
Ethernet configuration, binary code).

Example config:

```json
{"pass_device": "D100", "fail_device": "D102", "width": 2,
 "job_device": "D200", "job_length": 8}
```

| Field | Meaning |
|---|---|
| `pass_device` / `fail_device` | Register with each counter, e.g. `D100` (D, W, R, ZR… supported) |
| `count_device` | Optional total counter |
| `width` | `1` = 16-bit, `2` = 32-bit (two words, low word first) |
| `job_device` | Optional first register of the job name or number |
| `job_length` | Words of an ASCII job name (2 characters per word, default 8) |
| `job_format` | `ascii` (text) or `number` (job id) |
| `default_job` | Job name when `job_device` is not set (default `MAIN`) |
| `transport` | `tcp` (default) or `udp`, as set on the PLC's SLMP port |
| `network` / `pc` / `module_io` / `station` | 3E routing (defaults 0 / 255 / 1023 / 0 = own station) |
| `timeout` | Seconds to wait for the PLC (default 3) |

## SLMP server (`slmp_listen`)

No PLC needed: **the app pretends to be the PLC**. Enter the server's IP and a
port from `LISTEN_PORTS` as the PLC address in the camera's SLMP settings.

- **`counter` mode** reads the running totals the camera writes.
- **`event` mode** counts one part each time `trigger_device` changes (for
  example a result ID) and uses `status_device` / `pass_values` to decide
  pass or fail.

| Field | Meaning |
|---|---|
| `mode` | `counter` or `event` |
| `pass_device` / `fail_device` / `count_device` | Counter mode registers |
| `width` | `1` = 16-bit, `2` = 32-bit (low word first) |
| `trigger_device` | Event mode: register that changes once per part |
| `status_device` | Event mode: register holding the part's result |
| `pass_values` | Event mode: status values meaning pass (default `[1]`) |
| `job_device` / `job_length` / `job_format` / `default_job` | As for the SLMP client |
| `preset` | Optional `{"D0": 1}` values the camera can read before writing |

## OPC UA client (`opcua`)

Reads the counters from any OPC UA server: a PLC (Siemens S7-1500, Beckhoff,
B&R, Omron, ...), an edge gateway such as Kepware, a machine controller, or a
camera or sensor with its own OPC UA server. The app is the client; each poll
connects, reads the configured nodes and disconnects. Counters are folded into
the reset-proof running totals like every other protocol.

The device form has its own fields for OPC UA (stored in the config):

| Field | Meaning |
|---|---|
| `endpoint` | The server's endpoint URL, e.g. `opc.tcp://10.0.0.5:4840`. The device list shows it as the address. |
| `security_mode` | `None`, `Sign` or `SignAndEncrypt` |
| `security_policy` | With Sign / SignAndEncrypt: `Basic256Sha256` (default), `Aes128_Sha256_RsaOaep`, `Aes256_Sha256_RsaPss`, or the older `Basic256` / `Basic128Rsa15` |
| `username` / `password` | Login; leave the username blank for anonymous. The password is never sent back to the browser or exported. |
| `pass_node` / `fail_node` | Node ids of the pass and fail counters, e.g. `ns=2;s=Line1.Pass` or `ns=3;i=1001` |
| `count_node` | Optional node id of a total counter (otherwise pass + fail) |
| `job_node` | Optional node id holding the job / recipe name or number |
| `default_job` | Job name when `job_node` is not set (default `MAIN`) |
| `timeout` | Connection timeout in seconds (default 5) |

**Browse…** next to each node field lists the server's address space from the
Objects folder down, with each variable's current value and data type; press
**Use** to take its node id. Browsing uses the endpoint, security and login
entered in the form, so it also tests them.

Counter nodes may be any numeric type (integers, floats, Boolean); job nodes
may be text or a number.

**Client certificate.** With Sign or SignAndEncrypt the app presents its own
certificate (application URI `urn:scrap-meter:opcua-client`, valid 10 years).
It is generated on first use and kept in `DATA_DIR/opcua` (the `app_data`
volume), so it stays the same across updates and the server has to trust it
only once. Many servers reject unknown client certificates at first: download
it with the link on the device form (or `GET /api/devices/opcua/certificate`)
and move it to the server's trusted list. The app accepts any server
certificate.

Common errors are reported in plain words on the device list: the server
refused the login, no endpoint with the chosen security mode and policy,
the certificate is not trusted, or a node id does not exist.

To try it without a real server, run the simulation server from the tests:
`python tests/opcua_sim.py --port 4840 --user operator:secret` serves
`ns=2;s=Line1.Pass`, `.Fail`, `.Total` and `.Job` with security None, Sign and
SignAndEncrypt.

## PROFINET (`profinet`): gateway mode only

PROFINET IO is a real-time protocol over raw layer-2 Ethernet frames, with
DCP discovery and GSDML-based configuration normally owned by a PLC. A
user-space Python app **cannot** act as a PROFINET IO controller, so this app
does **not** speak native PROFINET.

The `profinet` driver only works in **gateway mode**: point it at a
PROFINET-to-Modbus/TCP gateway, or at the controlling PLC's data exposed over
Modbus/TCP, and it reads the counters from there. Any other `mode` fails with
an error instead of pretending to work.

| Field | Meaning |
|---|---|
| `mode` | Must be `gateway` |
| `gateway_host` / `gateway_port` | The gateway or PLC Modbus endpoint (port default 502) |
| `modbus` | Register map, same fields as the Modbus driver |

## Simulated device (`simulator`)

Needs no hardware. Fabricates a counting job that periodically resets and
changes job, which is useful for demos and for trying out alerts.

| Field | Meaning |
|---|---|
| `jobs` | List of job names to cycle through |
| `fail_ratio` | Fraction of parts that fail (0-1) |
| `parts_per_poll` | Parts produced between polls |
| `reset_every` | Reset raw counters every N reads (0 = never) |
| `job_change_every` | Switch job every N reads (0 = never) |
