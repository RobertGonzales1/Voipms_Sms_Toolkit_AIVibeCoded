#!/usr/bin/env python3
"""
sip_keepalive.py - keep VoIP.ms subaccounts continuously SIP-registered.

VoIP.ms will only deliver inbound SMS to a subaccount that is registered at one
of their POPs. An unregistered subaccount causes the delivery attempt to fail,
which surfaces to the sender as a rejected message. This daemon holds the
registration open so that never happens.

It does three things per account:
  1. REGISTER (with MD5 digest auth) and re-register before the grant expires.
  2. Send a NAT keepalive so the UDP pinhole the server replies through stays open.
     A registration that the server cannot actually reach is worse than useless.
  3. Answer inbound SIP requests:
       MESSAGE -> 200 OK   (this is what ACCEPTS an inbound SMS)
       OPTIONS -> 200 OK   (server reachability probe)
       INVITE  -> 480 by default, so DID failover behaves as it does today

Stdlib only - no pip install required.

Usage:
    python sip_keepalive.py --check        # register once, report, deregister, exit
    python sip_keepalive.py                # run forever (this is the daemon)
    python sip_keepalive.py --status       # print the last status written by a running daemon
    python sip_keepalive.py --verbose      # log full SIP traffic

Exit codes:  0 = ok   1 = one or more accounts failed   3 = config/tool error
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import secrets
import select
import socket
import string
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "sip_config.json")
STATUS_PATH = os.path.join(HERE, "sip_status.json")
LOG_PATH = os.path.join(HERE, "sip_keepalive.log")
SMS_LOG_PATH = os.path.join(HERE, "sip_messages.log")
# Touching this file asks the daemon to shut down cleanly (and deregister).
# Windows has no practical way to send Ctrl+C to a background child process.
STOP_FLAG_PATH = os.path.join(HERE, "sip_stop.flag")

CRLF = "\r\n"
USER_AGENT = "voipms-keepalive/1.0"

# VoIP.ms accepts 60..3600 for Expires. 300 is their own recommendation: short
# enough to recover fast from a dropped registration, long enough to be quiet.
DEFAULT_EXPIRES = 300
DEFAULT_NAT_KEEPALIVE = 30
DEFAULT_SIP_PORT = 5060

# Logs are append-only and this daemon runs forever, so cap them. One previous
# generation is kept as <name>.1.
MAX_LOG_BYTES = 2 * 1024 * 1024

_log_lock = threading.Lock()
_shutdown = threading.Event()


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

def _rotate_if_needed(path: str) -> None:
    """Caller must hold _log_lock."""
    try:
        if os.path.getsize(path) < MAX_LOG_BYTES:
            return
        os.replace(path, path + ".1")
    except OSError:
        pass


def log(message: str, *, path: str = LOG_PATH, echo: bool = True) -> None:
    stamp = dt.datetime.now().replace(microsecond=0).isoformat()
    line = f"{stamp}  {message}"
    if echo:
        print(line, flush=True)
    with _log_lock:
        try:
            _rotate_if_needed(path)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def resolve_password(entry: dict) -> str:
    """Password from the environment first, then the config file.

    Env vars keep the SIP password out of sip_config.json, which matters because
    this folder is typically inside a cloud-synced directory. A SIP password
    permits outbound calling at the account owner's expense, so it is the more
    sensitive of the two credentials this toolkit handles.
    """
    def env_key(value: str) -> str:
        return "VOIPMS_SIP_PASSWORD_" + re.sub(r"[^A-Za-z0-9]", "_", str(value)).upper()

    for candidate in (entry.get("label"), entry.get("account")):
        if candidate:
            value = os.environ.get(env_key(candidate))
            if value:
                return value
    return str(entry.get("password") or "")


# --------------------------------------------------------------------------
# digest auth
# --------------------------------------------------------------------------

def _md5(text: str) -> str:
    try:
        return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()
    except TypeError:  # older Python without the keyword
        return hashlib.md5(text.encode("utf-8")).hexdigest()


def parse_auth_header(value: str) -> dict:
    """Parse a WWW-Authenticate / Proxy-Authenticate digest challenge."""
    value = value.strip()
    if value.lower().startswith("digest "):
        value = value[7:]

    params, key, buf, in_quotes, escaped = {}, None, [], False, False
    for char in value:
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\" and in_quotes:
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
        elif char == "=" and key is None and not in_quotes:
            key = "".join(buf).strip().lower()
            buf = []
        elif char == "," and not in_quotes:
            if key is not None:
                params[key] = "".join(buf).strip()
            key, buf = None, []
        else:
            buf.append(char)
    if key is not None:
        params[key] = "".join(buf).strip()
    return params


def build_authorization(username, password, challenge, method, uri, nonce_count):
    """Build an Authorization header value for a digest challenge."""
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    opaque = challenge.get("opaque")
    algorithm = challenge.get("algorithm", "MD5")
    qop_options = [q.strip() for q in challenge.get("qop", "").split(",") if q.strip()]

    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")

    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
    ]

    if "auth" in qop_options:
        cnonce = secrets.token_hex(8)
        nc = f"{nonce_count:08x}"
        response = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        parts += [f'response="{response}"', "qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        response = _md5(f"{ha1}:{nonce}:{ha2}")
        parts.append(f'response="{response}"')

    parts.append(f"algorithm={algorithm}")
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)


# --------------------------------------------------------------------------
# SIP message helpers
# --------------------------------------------------------------------------

def parse_sip(data: str) -> dict:
    """Split a SIP message into start line, headers dict, and body."""
    head, _, body = data.partition(CRLF + CRLF)
    lines = head.split(CRLF)
    start = lines[0] if lines else ""

    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            continue
        key = name.strip().lower()
        value = value.strip()
        # Multiple Via headers matter; keep the first, which is ours.
        headers[key] = value if key not in headers else headers[key] + "," + value

    status_code = None
    if start.startswith("SIP/2.0"):
        chunks = start.split(None, 2)
        if len(chunks) >= 2 and chunks[1].isdigit():
            status_code = int(chunks[1])

    return {
        "start": start,
        "method": start.split(None, 1)[0] if start and status_code is None else None,
        "status": status_code,
        "headers": headers,
        "body": body,
    }


def new_branch() -> str:
    return "z9hG4bK" + secrets.token_hex(8)


def new_tag() -> str:
    return secrets.token_hex(6)


def new_call_id() -> str:
    return secrets.token_hex(12) + "@voipms-keepalive"


def random_username_suffix() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


# --------------------------------------------------------------------------
# account worker
# --------------------------------------------------------------------------

class SipAccount:
    """Holds one subaccount registered, and answers what the server sends it."""

    def __init__(self, entry: dict, defaults: dict):
        self.username = str(entry["account"]).strip()
        self.password = resolve_password(entry)
        self.server = str(entry.get("server") or defaults.get("server", "")).strip()
        self.label = str(entry.get("label") or self.username)
        self.port = int(entry.get("sip_port") or defaults.get("sip_port", DEFAULT_SIP_PORT))
        self.expires = int(entry.get("expires") or defaults.get("expires", DEFAULT_EXPIRES))
        self.nat_interval = int(
            entry.get("nat_keepalive") or defaults.get("nat_keepalive", DEFAULT_NAT_KEEPALIVE)
        )
        self.invite_response = str(
            entry.get("invite_response") or defaults.get("invite_response", "480 Temporarily Unavailable")
        )
        self.verbose = bool(defaults.get("verbose"))
        # Message bodies are 2FA codes and password resets more often than not.
        # Off by default: sender + timestamp alone still proves whether a message
        # reached VoIP.ms, without retaining the secret.
        self.log_bodies = bool(defaults.get("log_message_bodies", False))

        # Phone number(s) this subaccount receives SMS for. Display only - accepts
        # a single "did" string or a "dids" list.
        raw_dids = entry.get("dids") or entry.get("did") or []
        if isinstance(raw_dids, (str, int)):
            raw_dids = [raw_dids]
        self.dids = [str(d).strip() for d in raw_dids if str(d).strip()]

        if not self.username or not self.password or not self.server:
            raise ValueError(f"account entry needs account, password and server: {entry!r}")
        self.expires = max(60, min(3600, self.expires))

        self.sock = None
        self.local_ip = None
        self.local_port = None
        self.server_addr = None
        self.call_id = new_call_id()
        self.from_tag = new_tag()
        self.cseq = 0
        self.nonce_count = 0
        # What the server actually granted, which can be less than we asked for.
        self.granted_expires = self.expires

        # shared with the status writer
        self.lock = threading.Lock()
        self.registered = False
        self.last_ok = None
        self.last_error = None
        self.registered_until = 0.0
        self.messages_received = 0

    # -- plumbing ---------------------------------------------------------

    def resolve_server(self) -> None:
        """Re-resolve the POP each cycle so a rotated IP is picked up."""
        self.server_addr = (socket.gethostbyname(self.server), self.port)

    def open_socket(self) -> None:
        self.resolve_server()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        # Learn which local address routes to this POP.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(self.server_addr)
            self.local_ip = probe.getsockname()[0]
        finally:
            probe.close()
        self.local_port = self.sock.getsockname()[1]

    def close_socket(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def from_server(self, addr) -> bool:
        """Reject datagrams that did not come from our POP.

        Without this, anything that reaches this ephemeral port is parsed and
        acted on: a forged MESSAGE would be answered 200 OK, counted, and logged
        as a genuine SMS. It also keeps stray traffic away from the parser.
        """
        if not self.server_addr or not addr:
            return False
        if addr[0] != self.server_addr[0]:
            if self.verbose:
                log(f"[{self.label}] dropped packet from {addr[0]}", echo=False)
            return False
        return True

    def send(self, message: str) -> None:
        if self.verbose:
            log(f"[{self.label}] >>>\n{message}", echo=False)
        self.sock.sendto(message.encode("utf-8"), self.server_addr)

    # -- requests ---------------------------------------------------------

    def build_register(self, authorization: str | None, expires: int) -> str:
        self.cseq += 1
        uri = f"sip:{self.server}"
        lines = [
            f"REGISTER {uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={new_branch()};rport",
            "Max-Forwards: 70",
            f"From: <sip:{self.username}@{self.server}>;tag={self.from_tag}",
            f"To: <sip:{self.username}@{self.server}>",
            f"Call-ID: {self.call_id}",
            f"CSeq: {self.cseq} REGISTER",
            f"Contact: <sip:{self.username}@{self.local_ip}:{self.local_port};transport=udp>",
            f"Expires: {expires}",
            f"User-Agent: {USER_AGENT}",
            "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, MESSAGE",
        ]
        if authorization:
            lines.append(f"Authorization: {authorization}")
        lines += ["Content-Length: 0", "", ""]
        return CRLF.join(lines)

    def build_response(self, request: dict, status_line: str, extra_headers=None) -> str:
        headers = request["headers"]
        lines = [f"SIP/2.0 {status_line}"]
        for key in ("via", "from", "to", "call-id", "cseq"):
            if key in headers:
                label = {"via": "Via", "from": "From", "to": "To",
                         "call-id": "Call-ID", "cseq": "CSeq"}[key]
                value = headers[key]
                # A response to an in-dialog-less request needs a To tag.
                if key == "to" and ";tag=" not in value:
                    value += f";tag={new_tag()}"
                lines.append(f"{label}: {value}")
        lines.append(f"User-Agent: {USER_AGENT}")
        for header in extra_headers or []:
            lines.append(header)
        lines += ["Content-Length: 0", "", ""]
        return CRLF.join(lines)

    # -- registration -----------------------------------------------------

    def do_register(self, expires: int) -> bool:
        """One REGISTER transaction, including the digest retry. True on success."""
        self.send(self.build_register(None, expires))
        challenge_used = False

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([self.sock], [], [], max(0.1, remaining))
            if not ready:
                continue

            data, addr = self.sock.recvfrom(65535)
            if not self.from_server(addr):
                continue
            text = data.decode("utf-8", errors="replace")
            if self.verbose:
                log(f"[{self.label}] <<<\n{text}", echo=False)
            msg = parse_sip(text)

            # The server can send us traffic mid-transaction; answer it.
            if msg["status"] is None:
                self.handle_request(msg)
                continue

            if msg["status"] in (401, 407) and not challenge_used:
                challenge_used = True
                header = msg["headers"].get("www-authenticate") or msg["headers"].get("proxy-authenticate")
                if not header:
                    self.set_error("challenge without an auth header")
                    return False
                self.nonce_count += 1
                auth = build_authorization(
                    self.username, self.password, parse_auth_header(header),
                    "REGISTER", f"sip:{self.server}", self.nonce_count,
                )
                self.send(self.build_register(auth, expires))
                deadline = time.monotonic() + 10
                continue

            if 200 <= msg["status"] < 300:
                granted = self.granted_expiry(msg, expires)
                # Refresh scheduling keys off this, not off what we requested.
                self.granted_expires = granted if expires > 0 else expires
                with self.lock:
                    self.registered = expires > 0
                    self.last_ok = time.time()
                    self.last_error = None
                    self.registered_until = time.time() + granted if expires > 0 else 0
                return True

            if msg["status"] in (401, 403):
                self.set_error(f"authentication rejected ({msg['status']}) - check the subaccount password")
                return False

            if msg["status"] >= 400:
                self.set_error(f"REGISTER failed with {msg['status']} {msg['start']}")
                return False

        self.set_error("no response from server (timeout)")
        return False

    def granted_expiry(self, msg: dict, requested: int) -> int:
        """Honour whatever the server actually granted, not what we asked for.

        Only the leading digit run after 'expires=' belongs to the value. Taking
        every digit in the tail would swallow following parameters, so
        ';expires=60;received=1.2.3.4' would read as 601234 seconds and the
        registration would silently lapse long before we refreshed it.
        """
        contact = msg["headers"].get("contact", "")
        match = re.search(r"expires\s*=\s*(\d+)", contact, re.IGNORECASE)
        if match:
            return int(match.group(1))

        header = msg["headers"].get("expires")
        if header and header.strip().isdigit():
            return int(header.strip())
        return requested

    def set_error(self, message: str) -> None:
        with self.lock:
            self.registered = False
            self.last_error = message
        log(f"[{self.label}] ERROR: {message}")

    # -- inbound ----------------------------------------------------------

    def handle_request(self, msg: dict) -> None:
        method = (msg["method"] or "").upper()

        if method == "MESSAGE":
            # Answering 200 OK is what accepts the inbound SMS. Without this the
            # server sees a failed delivery and the sender gets a rejection.
            self.send(self.build_response(msg, "200 OK"))
            sender = msg["headers"].get("from", "")
            body = msg["body"].strip()
            with self.lock:
                self.messages_received += 1
            log(f"[{self.label}] SMS accepted from {sender}")
            record = body if self.log_bodies else f"<{len(body)} chars, body logging off>"
            log(f"{self.label}\t{sender}\t{record}", path=SMS_LOG_PATH, echo=False)

        elif method == "OPTIONS":
            self.send(self.build_response(msg, "200 OK"))

        elif method == "INVITE":
            # We are not a phone. Decline in a way that lets DID failover apply.
            self.send(self.build_response(msg, self.invite_response))
            log(f"[{self.label}] call declined with '{self.invite_response}'")

        elif method in ("ACK", "CANCEL", "BYE"):
            if method != "ACK":
                self.send(self.build_response(msg, "200 OK"))

        elif method:
            self.send(self.build_response(msg, "405 Method Not Allowed"))

    # -- main loop --------------------------------------------------------

    def run(self) -> None:
        backoff = 5
        while not _shutdown.is_set():
            try:
                if self.sock is None:
                    self.open_socket()
                    log(f"[{self.label}] socket {self.local_ip}:{self.local_port} -> "
                        f"{self.server} ({self.server_addr[0]})")
                else:
                    self.resolve_server()

                if not self.do_register(self.expires):
                    self.close_socket()
                    _shutdown.wait(backoff)
                    backoff = min(backoff * 2, 300)
                    continue

                backoff = 5
                log(f"[{self.label}] registered (expires in {self.granted_expires}s)")
                self.service_registration()

            except (OSError, socket.gaierror) as exc:
                self.set_error(f"network: {exc}")
                self.close_socket()
                _shutdown.wait(backoff)
                backoff = min(backoff * 2, 300)

            except Exception as exc:  # noqa: BLE001 - see below
                # Anything unexpected must NOT kill this thread. If it did, the
                # account would stop re-registering while snapshot() kept
                # reporting the last known 'registered' state - the dashboard
                # would show green while inbound SMS was being rejected.
                self.set_error(f"unexpected: {type(exc).__name__}: {exc}")
                self.close_socket()
                _shutdown.wait(backoff)
                backoff = min(backoff * 2, 300)

        self.deregister()
        self.close_socket()

    def mark_thread_dead(self) -> None:
        """Called by the supervisor if this account's worker is gone."""
        with self.lock:
            self.registered = False
            self.registered_until = 0.0
            self.last_error = "worker thread died - registration is not being renewed"

    def service_registration(self) -> None:
        """Answer traffic and keep NAT open until it is time to re-register."""
        # Refresh early so a lost packet does not cost us the registration, and
        # key off what the server granted - it may have given us less than we
        # asked for, in which case refreshing on our own figure lapses first.
        lifetime = max(30, int(self.granted_expires * 0.6))
        refresh_at = time.monotonic() + lifetime
        next_ping = time.monotonic() + self.nat_interval

        while not _shutdown.is_set():
            now = time.monotonic()
            if now >= refresh_at:
                return

            # Wake at least once a second so a shutdown request is noticed
            # promptly. Sleeping all the way to next_ping (up to nat_keepalive
            # seconds) would delay deregistration past the caller's patience.
            timeout = min(1.0, max(0.1, min(refresh_at, next_ping) - now))
            ready, _, _ = select.select([self.sock], [], [], timeout)

            if ready:
                try:
                    data, addr = self.sock.recvfrom(65535)
                except OSError:
                    return
                if not self.from_server(addr):
                    continue
                text = data.decode("utf-8", errors="replace")
                if self.verbose:
                    log(f"[{self.label}] <<<\n{text}", echo=False)
                msg = parse_sip(text)
                if msg["status"] is None:
                    self.handle_request(msg)

            if time.monotonic() >= next_ping:
                try:
                    # A bare CRLF is the standard SIP NAT keepalive: it refreshes
                    # the router's UDP binding without creating a transaction.
                    self.sock.sendto(b"\r\n\r\n", self.server_addr)
                except OSError:
                    return
                next_ping = time.monotonic() + self.nat_interval

    def deregister(self) -> None:
        if self.sock and self.registered:
            try:
                self.do_register(0)
                log(f"[{self.label}] deregistered")
            except OSError:
                pass

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "label": self.label,
                "account": self.username,
                "dids": self.dids,
                "server": self.server,
                "registered": self.registered,
                "last_ok": dt.datetime.fromtimestamp(self.last_ok).isoformat(timespec="seconds")
                           if self.last_ok else None,
                "seconds_until_expiry": max(0, int(self.registered_until - time.time()))
                                        if self.registered_until else 0,
                # Absolute deadline too, so a viewer can count down smoothly
                # between status writes instead of seeing it jump.
                "expires_at": dt.datetime.fromtimestamp(self.registered_until).isoformat(timespec="seconds")
                              if self.registered_until else None,
                # Pre-formatted here so number formatting has one implementation
                # rather than one per viewer. A list, so a viewer can lay several
                # numbers out across rows if it wants to.
                "display": [format_did(d) for d in self.dids] or ["(no DID set)"],
                "messages_received": self.messages_received,
                "last_error": self.last_error,
            }


# --------------------------------------------------------------------------
# config + orchestration
# --------------------------------------------------------------------------

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise RuntimeError(
            f"Missing {CONFIG_PATH}\n"
            "  Copy sip_config.example.json to sip_config.json and fill in your\n"
            "  subaccount names, SIP passwords and POP server."
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        raw = fh.read()

    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        # The bare message ("Expecting ',' delimiter: line 17 column 7") does not
        # say which file, and hand-edited JSON is the most common way to land
        # here. Show the offending line with a caret under the column.
        lines = raw.splitlines()
        pointer = ""
        if 1 <= exc.lineno <= len(lines):
            offending = lines[exc.lineno - 1]
            pointer = f"\n\n    {offending}\n    {' ' * max(0, exc.colno - 1)}^"
        raise RuntimeError(
            f"{CONFIG_PATH} is not valid JSON.\n"
            f"  {exc.msg} at line {exc.lineno}, column {exc.colno}.{pointer}\n\n"
            "  Usual causes: a trailing comma before a } or ], a missing comma\n"
            "  between entries, or a smart quote pasted in place of a plain \"."
        ) from exc

    if not isinstance(cfg, dict):
        raise RuntimeError(f"{CONFIG_PATH} must contain a JSON object")
    if not cfg.get("accounts"):
        raise RuntimeError("sip_config.json has no 'accounts' entries")

    # A SIP password in a cloud-synced folder is a password uploaded to a third
    # party. Warn rather than refuse - the env-var path is right there.
    if "onedrive" in CONFIG_PATH.lower() or "dropbox" in CONFIG_PATH.lower():
        if any(entry.get("password") for entry in cfg["accounts"]):
            log("WARNING: sip_config.json holds passwords and sits in a cloud-synced "
                "folder. Prefer VOIPMS_SIP_PASSWORD_<LABEL> environment variables.")
    return cfg


def write_status(accounts: list) -> None:
    payload = {
        "updated": dt.datetime.now().replace(microsecond=0).isoformat(),
        "pid": os.getpid(),
        "accounts": [acct.snapshot() for acct in accounts],
    }
    tmp = STATUS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, STATUS_PATH)
    except OSError:
        pass


def format_did(did: str) -> str:
    """Render a NANP number readably; leave anything else alone."""
    digits = "".join(c for c in str(did) if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return str(did)


def cmd_status() -> int:
    if not os.path.exists(STATUS_PATH):
        print("No sip_status.json - the keepalive daemon has not run yet.")
        return 3
    with open(STATUS_PATH, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    age = "unknown"
    try:
        updated = dt.datetime.fromisoformat(payload["updated"])
        age = f"{int((dt.datetime.now() - updated).total_seconds())}s ago"
    except (ValueError, KeyError):
        pass

    print(f"Status written {age} (pid {payload.get('pid')})")
    print("-" * 72)
    bad = 0
    for acct in payload.get("accounts", []):
        numbers = ", ".join(acct.get("display") or []) or acct["account"]
        if acct["registered"]:
            print(f"[ok] {numbers:<20} {acct['label']:<14} registered, renews in "
                  f"{acct['seconds_until_expiry']}s  msgs={acct['messages_received']}")
        else:
            bad += 1
            print(f"[!!] {numbers:<20} {acct['label']:<14} NOT REGISTERED - "
                  f"{acct.get('last_error') or 'unknown'}")
    print("-" * 72)
    return 1 if bad else 0


def cmd_check(cfg: dict, verbose: bool) -> int:
    """Register each account once, report, then cleanly deregister."""
    defaults = dict(cfg.get("defaults", {}))
    defaults["verbose"] = verbose
    failures = 0

    for entry in cfg["accounts"]:
        acct = SipAccount(entry, defaults)
        try:
            acct.open_socket()
            ok = acct.do_register(acct.expires)
            if ok:
                print(f"[ok] {acct.label:<24} registered via {acct.server}")
                acct.do_register(0)
            else:
                failures += 1
                print(f"[!!] {acct.label:<24} FAILED - {acct.last_error}")
        except (OSError, socket.gaierror) as exc:
            failures += 1
            print(f"[!!] {acct.label:<24} FAILED - {exc}")
        finally:
            acct.close_socket()

    print()
    print("All accounts registered successfully." if not failures
          else f"{failures} account(s) failed.")
    return 1 if failures else 0


def cmd_run(cfg: dict, verbose: bool) -> int:
    defaults = dict(cfg.get("defaults", {}))
    defaults["verbose"] = verbose

    accounts = [SipAccount(entry, defaults) for entry in cfg["accounts"]]
    threads = [threading.Thread(target=a.run, name=a.label, daemon=True) for a in accounts]

    log(f"starting keepalive for {len(accounts)} account(s)")
    for thread in threads:
        thread.start()

    # A stale flag from a previous hard kill would stop us instantly.
    if os.path.exists(STOP_FLAG_PATH):
        try:
            os.remove(STOP_FLAG_PATH)
        except OSError:
            pass

    # Write whenever the state actually changes, plus a heartbeat every 15s.
    # Polling on a fixed interval alone would leave a viewer showing stale
    # "not registered" for up to 15s right after startup; writing every second
    # instead would churn this folder's OneDrive sync for no benefit.
    last_signature = None
    last_write = 0.0

    try:
        while not _shutdown.is_set():
            # A worker that has died stops renewing its registration, but its
            # last snapshot would keep reporting 'registered'. Catch that here so
            # the status file cannot show green for an account nobody is serving.
            for acct, thread in zip(accounts, threads):
                if not thread.is_alive():
                    with acct.lock:
                        already = acct.last_error == "worker thread died - registration is not being renewed"
                    if not already:
                        log(f"[{acct.label}] worker thread is gone - marking unregistered")
                    acct.mark_thread_dead()

            snapshots = [acct.snapshot() for acct in accounts]
            signature = json.dumps([
                [s["registered"], s["messages_received"], s["last_error"], s["expires_at"]]
                for s in snapshots
            ])
            now = time.time()
            if signature != last_signature or now - last_write >= 15:
                write_status(accounts)
                last_signature, last_write = signature, now

            if os.path.exists(STOP_FLAG_PATH):
                log("stop requested - shutting down")
                try:
                    os.remove(STOP_FLAG_PATH)
                except OSError:
                    pass
                _shutdown.set()
                break
            _shutdown.wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        log("shutting down - deregistering")
        _shutdown.set()
        for thread in threads:
            thread.join(timeout=15)
        write_status(accounts)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Keep VoIP.ms subaccounts SIP-registered")
    parser.add_argument("--check", action="store_true", help="register once, report, exit")
    parser.add_argument("--status", action="store_true", help="print the running daemon's status")
    parser.add_argument("--verbose", action="store_true", help="log full SIP traffic")
    args = parser.parse_args()

    try:
        if args.status:
            return cmd_status()
        cfg = load_config()
        if args.check:
            return cmd_check(cfg, args.verbose)
        return cmd_run(cfg, args.verbose)
    except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
