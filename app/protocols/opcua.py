"""OPC UA client driver: reads counters from any OPC UA server (a PLC, an
edge gateway, a machine controller, a camera with an OPC UA server).

The app is the OPC UA *client*. The device holds only the connection; each
station that uses it picks the node it needs (its OK, NOK, total or job
value), and each poll connects, reads exactly those nodes (``read_values``),
and disconnects. Config::

    config = {
        "endpoint": "opc.tcp://10.0.0.5:4840",  # server endpoint URL
        "security_mode": "None",            # "None", "Sign" or "SignAndEncrypt"
        "security_policy": "Basic256Sha256",  # used with Sign / SignAndEncrypt
        "username": "",                     # blank = anonymous login
        "password": "",
        "timeout": 5
    }

Version 1.4 also had "pass_node", "fail_node", "count_node", "job_node" and
"default_job" here; they still work (as the values "pass", "fail", "count",
"job" and ``read``), and the upgrade to stations moves them to the station.

Node ids use the standard string form: ``ns=2;s=Line1.Pass``, ``ns=3;i=1001``,
``i=2258``. The Browse helper (``browse``) walks the server's address space
to pick them.

Security: with Sign or SignAndEncrypt the app presents its own client
certificate. It is generated once (valid 10 years) and kept in DATA_DIR/opcua,
the data volume, so it survives updates and the server has to trust it only
once. Download it from the device form (``certificate_der``) and put it in the
server's trusted certificates if the server does not accept unknown clients.

Counters may be any numeric type (Int16..UInt64, Float, Double, Boolean); the
reset-proof accumulation in app.counters works on them like on every other
protocol.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
from pathlib import Path
from urllib.parse import urlparse

from ..config import settings
from ..counters import Sample
from .base import ProtocolDriver, ProtocolError

SECURITY_MODES = ("None", "Sign", "SignAndEncrypt")
SECURITY_POLICIES = (
    "Basic256Sha256",
    "Aes128_Sha256_RsaOaep",
    "Aes256_Sha256_RsaPss",
    "Basic256",
    "Basic128Rsa15",
)
APP_URI = "urn:scrap-meter:opcua-client"
DEFAULT_PORT = 4840
CERT_DAYS = 3650
MAX_BROWSE = 500  # children listed per node

_cert_lock = threading.Lock()

# asyncua logs a warning on every connect (session timeout trimmed by the
# server, certificate host name vs. the container's); errors still show
logging.getLogger("asyncua").setLevel(logging.ERROR)


# --------------------------------------------------------------------------- #
# Client certificate
# --------------------------------------------------------------------------- #


def cert_dir() -> Path:
    return Path(settings.data_dir) / "opcua"


def cert_paths() -> tuple[Path, Path]:
    d = cert_dir()
    return d / "client_cert.der", d / "client_key.pem"


def ensure_certificate() -> tuple[Path, Path]:
    """Create the client certificate and key once; later calls reuse them."""
    cert, key = cert_paths()
    with _cert_lock:
        if cert.is_file() and key.is_file():
            return cert, key
        from asyncua.crypto import cert_gen
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
        from cryptography.x509.oid import ExtendedKeyUsageOID

        cert.parent.mkdir(parents=True, exist_ok=True)
        private_key = cert_gen.generate_private_key()
        host = socket.gethostname() or "scrap-meter"
        certificate = cert_gen.generate_self_signed_app_certificate(
            private_key,
            APP_URI,
            {"commonName": "Scrap Meter", "organizationName": "Scrap Meter"},
            [x509.UniformResourceIdentifier(APP_URI), x509.DNSName(host)],
            extended=[ExtendedKeyUsageOID.CLIENT_AUTH],
            days=CERT_DAYS,
        )
        key.write_bytes(cert_gen.dump_private_key_as_pem(private_key))
        cert.write_bytes(certificate.public_bytes(Encoding.DER))
        return cert, key


def certificate_der() -> bytes:
    cert, _ = ensure_certificate()
    return cert.read_bytes()


# --------------------------------------------------------------------------- #
# Connection helpers (shared by the driver and the Browse helper)
# --------------------------------------------------------------------------- #


def endpoint_url(host: str, port: int, config: dict) -> str:
    url = (config.get("endpoint") or "").strip()
    if url:
        return url
    if not host:
        raise ProtocolError("Set the server's endpoint URL, e.g. opc.tcp://10.0.0.5:4840")
    return f"opc.tcp://{host}:{port or DEFAULT_PORT}"


def split_endpoint(url: str) -> tuple[str, int]:
    """Host and port of an opc.tcp:// URL (shown in the device list)."""
    parsed = urlparse(url.strip())
    if parsed.scheme != "opc.tcp" or not parsed.hostname:
        raise ProtocolError(f"The endpoint URL must look like opc.tcp://host:4840, not {url!r}")
    return parsed.hostname, parsed.port or DEFAULT_PORT


def _make_client(url: str, config: dict):
    try:
        from asyncua import Client, ua
        from asyncua.crypto import security_policies
    except ImportError as exc:  # pragma: no cover
        raise ProtocolError("asyncua is not installed (pip install asyncua)") from exc

    client = Client(url, timeout=float(config.get("timeout") or 5))
    client.application_uri = APP_URI
    client.name = "Scrap Meter"
    client.description = "Scrap Meter"

    mode = config.get("security_mode") or "None"
    if mode not in SECURITY_MODES:
        raise ProtocolError(f"Unknown security mode {mode!r}; use one of {', '.join(SECURITY_MODES)}")
    if mode != "None":
        policy_name = config.get("security_policy") or "Basic256Sha256"
        if policy_name not in SECURITY_POLICIES:
            raise ProtocolError(
                f"Unknown security policy {policy_name!r}; use one of {', '.join(SECURITY_POLICIES)}")
        policy = getattr(security_policies, "SecurityPolicy" + policy_name.replace("_", ""))
        cert, key = ensure_certificate()
        client._security_args = (policy, str(cert), str(key), getattr(ua.MessageSecurityMode, mode))

    if config.get("username"):
        client.set_user(str(config["username"]))
        client.set_password(str(config.get("password") or ""))
    return client


async def _connect(client) -> None:
    args = getattr(client, "_security_args", None)
    if args:
        policy, cert, key, mode = args
        await client.set_security(policy, cert, key, mode=mode)
    await client.connect()


def _explain(exc: Exception, url: str) -> str:
    text = str(exc) or exc.__class__.__name__
    name = exc.__class__.__name__
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"No answer from {url} (timeout)"
    if isinstance(exc, (ConnectionRefusedError, OSError)) and not text.startswith("Bad"):
        return f"Could not connect to {url}: {text}"
    if "BadUserAccessDenied" in name or "BadIdentityTokenRejected" in name or "BadIdentityTokenInvalid" in name:
        return "The server refused the login (check username and password, or allow anonymous login)"
    if "BadSecurity" in name or "BadCertificate" in name:
        return (f"The server refused the secure connection ({name}). Trust the app's client "
                "certificate on the server (download it from the device form), or check the security policy.")
    if "No matching endpoints" in text or "endpoint" in text.lower() and "match" in text.lower():
        return (f"The server offers no endpoint with this security mode and policy. "
                f"Pick another combination ({text})")
    return f"{name}: {text}"


def _run(coro, url: str, timeout: float):
    try:
        return asyncio.run(asyncio.wait_for(coro, timeout))
    except ProtocolError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalise driver errors
        raise ProtocolError(_explain(exc, url)) from exc


def _to_int(value, node_id: str) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise ProtocolError(f"Node {node_id} holds {value!r}, not a number") from None


def _plain(value):
    """A node value as JSON can store it: numbers, text and booleans stay,
    anything else (LocalizedText, dates, arrays) becomes text."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return _to_text(value)


def _to_text(value) -> str:
    text = getattr(value, "Text", None)  # LocalizedText
    if text is not None:
        value = text
    if isinstance(value, bytes):
        value = value.split(b"\x00")[0].decode("ascii", "replace")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


class OpcUaDriver(ProtocolDriver):
    key = "opcua"
    label = "OPC UA client"
    config_fields = {
        "endpoint": "Server endpoint URL, e.g. opc.tcp://10.0.0.5:4840",
        "security_mode": "None, Sign or SignAndEncrypt",
        "security_policy": "Basic256Sha256 (default), Aes128_Sha256_RsaOaep, Aes256_Sha256_RsaPss, "
                           "Basic256 or Basic128Rsa15; used with Sign / SignAndEncrypt",
        "username": "Login name; blank = anonymous",
        "password": "Login password",
        "timeout": "Connection timeout in seconds (default 5)",
    }
    #: stations pick any node of the server, not only pass/fail/count/job
    any_value = True

    # 1.4 config keys for the fixed values
    LEGACY = {"pass": "pass_node", "fail": "fail_node", "count": "count_node", "job": "job_node"}

    def read_values(self, keys: set[str]) -> dict:
        """Read the given node ids (and the 1.4 pass/fail/... nodes if set).

        A node that cannot be read is left out and listed in "_errors"; a
        failed connection raises ProtocolError.
        """
        cfg = self.config
        nodes = {k: k for k in keys if k and k not in self.LEGACY}
        for key, cfg_key in self.LEGACY.items():
            if (cfg.get(cfg_key) or "").strip():
                nodes[key] = cfg[cfg_key].strip()
        url = endpoint_url(self.host, self.port, cfg)
        timeout = float(cfg.get("timeout") or 5)
        return _run(self._read_values(url, nodes), url, timeout * 3)

    async def _read_values(self, url: str, nodes: dict[str, str]) -> dict:
        client = _make_client(url, self.config)
        await _connect(client)
        values, errors = {}, {}
        try:
            if not nodes:  # no station uses the server yet: just check it answers
                await client.get_node("i=2258").read_value()
            read: dict[str, object] = {}  # each node once, also when two keys name it
            for key, node_id in nodes.items():
                try:
                    if node_id not in read:
                        read[node_id] = _plain(await client.get_node(node_id).read_value())
                    values[key] = read[node_id]
                except Exception as exc:  # noqa: BLE001
                    errors[key] = f"Could not read {node_id}: {exc.__class__.__name__} {exc}".strip()
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if "job" in nodes and "job" in values:
            values["job"] = _to_text(values["job"])
        if errors:
            values["_errors"] = errors
        return values

    def read(self) -> Sample:
        cfg = self.config
        if not cfg.get("pass_node") and not cfg.get("fail_node"):
            raise ProtocolError("Set at least the pass or fail node id (use Browse on the device form)")
        url = endpoint_url(self.host, self.port, cfg)
        timeout = float(cfg.get("timeout") or 5)
        return _run(self._read(url), url, timeout * 3)

    async def _read(self, url: str) -> Sample:
        cfg = self.config
        client = _make_client(url, cfg)
        await _connect(client)
        try:
            async def value(key: str):
                node_id = (cfg.get(key) or "").strip()
                if not node_id:
                    return None
                try:
                    return await client.get_node(node_id).read_value()
                except Exception as exc:  # noqa: BLE001
                    raise ProtocolError(f"Could not read {key.replace('_', ' ')} {node_id}: "
                                        f"{exc.__class__.__name__} {exc}".strip()) from exc

            raw = {}
            for key in ("pass_node", "fail_node", "count_node"):
                v = await value(key)
                raw[key] = _to_int(v, cfg[key]) if v is not None else 0
            job_value = await value("job_node")
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

        job = _to_text(job_value) if job_value is not None else ""
        return Sample(
            job_name=job or str(cfg.get("default_job") or "MAIN"),
            raw_pass=raw["pass_node"],
            raw_fail=raw["fail_node"],
            raw_count=raw["count_node"],
            extra={"endpoint": url},
        )


# --------------------------------------------------------------------------- #
# Browse helper (device form)
# --------------------------------------------------------------------------- #


def browse(url: str, config: dict, node_id: str | None = None) -> dict:
    """The node and its children, for picking node ids on the device form.

    Starts at the Objects folder (i=85) when no node is given.
    """
    timeout = float(config.get("timeout") or 5)
    return _run(_browse(url, config, node_id or "i=85"), url, timeout * 4)


async def _browse(url: str, config: dict, node_id: str) -> dict:
    from asyncua import ua

    client = _make_client(url, config)
    await _connect(client)
    try:
        try:
            node = client.get_node(node_id)
            name = (await node.read_browse_name()).Name
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(f"Node {node_id} not found: {exc.__class__.__name__}") from exc
        children = []
        for child in (await node.get_children())[:MAX_BROWSE]:
            item = {"node_id": child.nodeid.to_string(), "name": "", "node_class": "", "value": None,
                    "data_type": None, "has_children": False}
            try:
                item["name"] = (await child.read_browse_name()).Name
                node_class = await child.read_node_class()
                item["node_class"] = node_class.name
                if node_class == ua.NodeClass.Variable:
                    dv = await child.read_data_value(raise_on_bad_status=False)
                    v = dv.Value.Value if dv.Value is not None else None
                    item["value"] = v if isinstance(v, (int, float, str, bool)) or v is None else _to_text(v)
                    try:
                        dt_node = client.get_node(await child.read_data_type())
                        item["data_type"] = (await dt_node.read_browse_name()).Name
                    except Exception:  # noqa: BLE001
                        pass
                item["has_children"] = node_class in (ua.NodeClass.Object, ua.NodeClass.View)
            except Exception:  # noqa: BLE001 - list what we can
                pass
            children.append(item)
        parent = None
        if node_id not in ("i=85", "i=84"):
            try:
                p = await node.get_parent()
                parent = p.nodeid.to_string() if p is not None else None
            except Exception:  # noqa: BLE001
                parent = None
        return {"node_id": node_id, "name": name, "parent": parent, "children": children}
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
