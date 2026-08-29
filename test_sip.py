#!/usr/bin/env python3
"""Tests for the SIP keepalive. No VoIP.ms account or outbound network needed.

Binds UDP sockets on 127.0.0.1 only. A fake registrar issues a digest challenge
and verifies our Authorization header the way a real registrar would.

Run:  python test_sip.py
      python -m unittest test_sip -v
      python -m unittest test_sip.ExpiryTests -v
"""

import os
import queue
import socket
import threading
import unittest

import sip_keepalive
from sip_keepalive import (
    CRLF,
    SipAccount,
    _md5,
    build_authorization,
    format_did,
    parse_auth_header,
    parse_sip,
    resolve_password,
)

REALM = "voip.ms"
NONCE = "3f1a9c2b7e4d6a8f"
PASSWORD = "s3cr3t-sip-pw"
USERNAME = "123456_test"


# --------------------------------------------------------------------------
# fake registrar
# --------------------------------------------------------------------------

class FakeRegistrar(threading.Thread):
    """Minimal SIP registrar: challenges once, then validates the digest."""

    def __init__(self, password=PASSWORD):
        super().__init__(daemon=True)
        self.password = password
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.client_addr = None
        self.digest_ok = None
        self.expires_seen = None
        self.contact_seen = None
        self.deregistered = False
        self.grant_expires = None      # override what we grant, for expiry tests
        self.stop = threading.Event()
        # Non-REGISTER traffic lands here so the reader thread and recv() never
        # race each other for the same datagram.
        self.inbox = queue.Queue()

    def run(self):
        self.sock.settimeout(0.25)
        challenged = False
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return

            self.client_addr = addr
            text = data.decode("utf-8", errors="replace")
            if not text.strip():
                continue  # NAT keepalive ping

            msg = parse_sip(text)
            if msg["method"] != "REGISTER":
                self.inbox.put(text)
                continue

            headers = msg["headers"]
            common = (
                f"Via: {headers.get('via','')}" + CRLF +
                f"From: {headers.get('from','')}" + CRLF +
                f"To: {headers.get('to','')};tag=srv" + CRLF +
                f"Call-ID: {headers.get('call-id','')}" + CRLF +
                f"CSeq: {headers.get('cseq','1 REGISTER')}" + CRLF
            )

            auth = headers.get("authorization")
            if not auth or not challenged:
                challenged = True
                self.sock.sendto((
                    "SIP/2.0 401 Unauthorized" + CRLF + common +
                    f'WWW-Authenticate: Digest realm="{REALM}", nonce="{NONCE}", '
                    'algorithm=MD5, qop="auth"' + CRLF +
                    "Content-Length: 0" + CRLF + CRLF
                ).encode(), addr)
                continue

            self.digest_ok = self._verify(auth)
            self.expires_seen = headers.get("expires")
            self.contact_seen = headers.get("contact")
            if self.expires_seen == "0":
                self.deregistered = True

            if not self.digest_ok:
                self.sock.sendto(("SIP/2.0 403 Forbidden" + CRLF + common +
                                  "Content-Length: 0" + CRLF + CRLF).encode(), addr)
                continue

            granted = self.grant_expires if self.grant_expires is not None else self.expires_seen
            self.sock.sendto((
                "SIP/2.0 200 OK" + CRLF + common +
                f"Contact: {self.contact_seen};expires={granted};received=127.0.0.1" + CRLF +
                "Content-Length: 0" + CRLF + CRLF
            ).encode(), addr)

    def _verify(self, header):
        p = parse_auth_header(header)
        ha1 = _md5(f"{p.get('username')}:{REALM}:{self.password}")
        ha2 = _md5(f"REGISTER:{p.get('uri')}")
        if p.get("qop") == "auth":
            want = _md5(f"{ha1}:{NONCE}:{p.get('nc')}:{p.get('cnonce')}:auth:{ha2}")
        else:
            want = _md5(f"{ha1}:{NONCE}:{ha2}")
        return want == p.get("response")

    def send_to_client(self, text):
        self.sock.sendto(text.encode(), self.client_addr)

    def recv(self, timeout=2.0):
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def shutdown(self):
        self.stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def make_account(server, port, password=PASSWORD, **extra):
    entry = {"account": USERNAME, "password": password, "label": "test"}
    entry.update(extra)
    return SipAccount(entry, {"server": server, "sip_port": port, "expires": 300})


# --------------------------------------------------------------------------

class DigestAuthTests(unittest.TestCase):

    def test_rfc2617_qop_auth_vector(self):
        ha1 = _md5("Mufasa:testrealm@host.com:Circle Of Life")
        ha2 = _md5("GET:/dir/index.html")
        got = _md5(f"{ha1}:dcd98b7102dd2f0e8b11d0f600bfb0c093:00000001:0a4f113b:auth:{ha2}")
        self.assertEqual(got, "6629fae49393a05397450978507c4ef1")

    def test_legacy_no_qop_vector(self):
        ha1 = _md5("Mufasa:testrealm@host.com:Circle Of Life")
        ha2 = _md5("GET:/dir/index.html")
        got = _md5(f"{ha1}:dcd98b7102dd2f0e8b11d0f600bfb0c093:{ha2}")
        self.assertEqual(got, "670fd8c2df070c60b045671b8b24ff02")

    def test_challenge_parsing(self):
        c = parse_auth_header(
            f'Digest realm="{REALM}", nonce="{NONCE}", algorithm=MD5, qop="auth"')
        self.assertEqual(c["realm"], REALM)
        self.assertEqual(c["nonce"], NONCE)
        self.assertEqual(c["qop"], "auth")
        self.assertEqual(c["algorithm"], "MD5")

    def test_separators_inside_quotes_survive(self):
        c = parse_auth_header('Digest realm="a,b", nonce="x=y", qop="auth"')
        self.assertEqual(c["realm"], "a,b")
        self.assertEqual(c["nonce"], "x=y")

    def test_no_qop_response_and_no_nc(self):
        challenge = parse_auth_header(f'Digest realm="{REALM}", nonce="{NONCE}"')
        value = build_authorization(USERNAME, PASSWORD, challenge, "REGISTER", "sip:voip.ms", 1)
        parsed = parse_auth_header(value)
        expected = _md5(
            f'{_md5(f"{USERNAME}:{REALM}:{PASSWORD}")}:{NONCE}:{_md5("REGISTER:sip:voip.ms")}')
        self.assertEqual(parsed["response"], expected)
        self.assertNotIn("nc", parsed)


class SipParsingTests(unittest.TestCase):

    def test_response_parsed(self):
        msg = parse_sip(
            "SIP/2.0 401 Unauthorized" + CRLF +
            'WWW-Authenticate: Digest realm="voip.ms", nonce="abc"' + CRLF + CRLF)
        self.assertEqual(msg["status"], 401)
        self.assertIsNone(msg["method"])
        self.assertIn("www-authenticate", msg["headers"])

    def test_request_parsed_with_body(self):
        msg = parse_sip(
            "MESSAGE sip:x@voip.ms SIP/2.0" + CRLF +
            "Content-Type: text/plain" + CRLF + CRLF + "hello world")
        self.assertEqual(msg["method"], "MESSAGE")
        self.assertIsNone(msg["status"])
        self.assertEqual(msg["body"], "hello world")


class ExpiryTests(unittest.TestCase):
    """Regression: only the leading digit run after 'expires=' is the value.

    Taking every digit in the tail read ';expires=60;received=1.2.3.4' as 601234
    seconds. Once refresh scheduling keys off this value, that silently parks the
    daemon for a week while the real registration lapses.
    """

    def setUp(self):
        self.account = make_account("127.0.0.1", 5060)

    def granted(self, contact, requested=300):
        return self.account.granted_expiry({"headers": {"contact": contact}}, requested)

    def test_plain_value(self):
        self.assertEqual(self.granted("<sip:u@1.2.3.4:5060>;expires=300"), 300)

    def test_trailing_parameter_is_not_absorbed(self):
        self.assertEqual(self.granted("<sip:u@1.2.3.4:5060>;expires=300;q=0.5"), 300)

    def test_trailing_received_ip_is_not_absorbed(self):
        self.assertEqual(self.granted("<sip:u@1.2.3.4:5060>;expires=60;received=1.2.3.4"), 60)

    def test_leading_parameter(self):
        self.assertEqual(self.granted("<sip:u@1.2.3.4:5060>;q=0.5;expires=120"), 120)

    def test_falls_back_to_expires_header(self):
        msg = {"headers": {"contact": "<sip:u@1.2.3.4:5060>", "expires": "90"}}
        self.assertEqual(self.account.granted_expiry(msg, 300), 90)

    def test_falls_back_to_requested(self):
        self.assertEqual(self.granted("<sip:u@1.2.3.4:5060>"), 300)


class SourceValidationTests(unittest.TestCase):
    """Regression: datagrams from anywhere but our POP must be dropped.

    Without this a forged MESSAGE would be answered 200 OK, counted, and written
    to the SMS log as a genuine message.
    """

    def setUp(self):
        self.account = make_account("127.0.0.1", 5060)
        self.account.server_addr = ("203.0.113.10", 5060)

    def test_accepts_server(self):
        self.assertTrue(self.account.from_server(("203.0.113.10", 5060)))

    def test_accepts_server_from_other_port(self):
        self.assertTrue(self.account.from_server(("203.0.113.10", 41234)))

    def test_rejects_other_host(self):
        self.assertFalse(self.account.from_server(("198.51.100.7", 5060)))

    def test_rejects_missing_address(self):
        self.assertFalse(self.account.from_server(None))

    def test_rejects_when_server_unresolved(self):
        self.account.server_addr = None
        self.assertFalse(self.account.from_server(("203.0.113.10", 5060)))


class PasswordResolutionTests(unittest.TestCase):

    def tearDown(self):
        for key in list(os.environ):
            if key.startswith("VOIPMS_SIP_PASSWORD_"):
                del os.environ[key]

    def test_config_value_used_when_no_env(self):
        self.assertEqual(resolve_password({"label": "main", "password": "from-file"}), "from-file")

    def test_env_by_label_wins(self):
        os.environ["VOIPMS_SIP_PASSWORD_MAIN"] = "from-env"
        self.assertEqual(resolve_password({"label": "main", "password": "from-file"}), "from-env")

    def test_env_by_account_when_no_label_match(self):
        os.environ["VOIPMS_SIP_PASSWORD_123456_BIZ"] = "from-env"
        self.assertEqual(
            resolve_password({"label": "business", "account": "123456_biz", "password": "x"}),
            "from-env")

    def test_label_punctuation_is_normalised(self):
        os.environ["VOIPMS_SIP_PASSWORD_MAIN_LINE"] = "from-env"
        self.assertEqual(resolve_password({"label": "main-line", "password": "x"}), "from-env")

    def test_missing_everywhere_is_empty(self):
        self.assertEqual(resolve_password({"label": "main"}), "")


class DisplayFormattingTests(unittest.TestCase):

    def test_nanp_ten_digit(self):
        self.assertEqual(format_did("5551234567"), "(555) 123-4567")

    def test_nanp_eleven_digit(self):
        self.assertEqual(format_did("15551234567"), "(555) 123-4567")

    def test_non_nanp_passes_through(self):
        self.assertEqual(format_did("442071234567"), "442071234567")

    def test_snapshot_carries_formatted_list(self):
        account = make_account("127.0.0.1", 5060, dids=["5551234567", "15559876543"])
        self.assertEqual(account.snapshot()["display"], ["(555) 123-4567", "(555) 987-6543"])

    def test_snapshot_placeholder_when_no_dids(self):
        self.assertEqual(make_account("127.0.0.1", 5060).snapshot()["display"], ["(no DID set)"])


class RegistrationHandshakeTests(unittest.TestCase):

    def setUp(self):
        self.server = FakeRegistrar()
        self.server.start()
        self.account = make_account("127.0.0.1", self.server.port)
        self.account.open_socket()

    def tearDown(self):
        self.account.close_socket()
        self.server.shutdown()

    def test_register_succeeds_and_digest_verifies(self):
        self.assertTrue(self.account.do_register(300))
        self.assertTrue(self.server.digest_ok)
        self.assertTrue(self.account.registered)
        self.assertEqual(self.server.expires_seen, "300")

    def test_contact_advertises_our_port(self):
        self.account.do_register(300)
        self.assertIn(f":{self.account.local_port}", self.server.contact_seen)

    def test_server_granted_expiry_is_honoured(self):
        """Regression: refresh must key off what the server granted.

        We ask for 300 and the registrar grants 60. Scheduling from our own
        request would refresh at 180s - two minutes after it had lapsed.
        """
        self.server.grant_expires = 60
        self.assertTrue(self.account.do_register(300))
        self.assertEqual(self.account.granted_expires, 60)
        self.assertLessEqual(self.account.registered_until - __import__("time").time(), 61)

    def test_deregister_sends_expires_zero(self):
        self.account.do_register(300)
        self.account.do_register(0)
        self.assertTrue(self.server.deregistered)
        self.assertFalse(self.account.registered)

    def test_wrong_password_is_rejected_and_reported(self):
        bad = make_account("127.0.0.1", self.server.port, password="wrong-password")
        bad.open_socket()
        try:
            self.assertFalse(bad.do_register(300))
            self.assertFalse(self.server.digest_ok)
            self.assertIn("authentication rejected", bad.last_error)
        finally:
            bad.close_socket()


class InboundRequestTests(unittest.TestCase):

    def setUp(self):
        self.server = FakeRegistrar()
        self.server.start()
        self.account = make_account("127.0.0.1", self.server.port)
        self.account.open_socket()
        self.account.do_register(300)
        self.logged = []
        self._real_log = sip_keepalive.log
        sip_keepalive.log = lambda msg, **kw: self.logged.append((kw.get("path"), msg))

    def tearDown(self):
        sip_keepalive.log = self._real_log
        self.account.close_socket()
        self.server.shutdown()

    def deliver(self, raw):
        """Push a request at the account and let it handle it."""
        self.server.send_to_client(raw)
        data, addr = self.account.sock.recvfrom(65535)
        self.assertTrue(self.account.from_server(addr))
        self.account.handle_request(parse_sip(data.decode()))
        return self.server.recv()

    def message(self, body="Your code is 4821"):
        return (
            f"MESSAGE sip:{USERNAME}@voip.ms SIP/2.0" + CRLF +
            f"Via: SIP/2.0/UDP 127.0.0.1:{self.server.port};branch=z9hG4bKmsg" + CRLF +
            "From: <sip:15551234567@voip.ms>;tag=abc" + CRLF +
            f"To: <sip:{USERNAME}@voip.ms>" + CRLF +
            "Call-ID: msg-test-1" + CRLF +
            "CSeq: 1 MESSAGE" + CRLF +
            "Content-Type: text/plain" + CRLF + CRLF + body
        )

    def test_message_answered_200(self):
        reply = self.deliver(self.message())
        self.assertIsNotNone(reply)
        self.assertIn("200 OK", reply.split(CRLF)[0])
        self.assertIn("msg-test-1", reply)
        self.assertEqual(self.account.messages_received, 1)

    def test_message_reply_carries_to_tag(self):
        reply = self.deliver(self.message())
        to_line = [l for l in reply.split(CRLF) if l.lower().startswith("to:")][0]
        self.assertIn(";tag=", to_line)

    def test_body_not_logged_by_default(self):
        """Regression: 2FA codes must not be written to disk unless asked for."""
        self.deliver(self.message("Your code is 4821"))
        sms_lines = [m for path, m in self.logged if path == sip_keepalive.SMS_LOG_PATH]
        self.assertTrue(sms_lines)
        self.assertNotIn("4821", " ".join(sms_lines))
        self.assertIn("body logging off", " ".join(sms_lines))

    def test_body_logged_when_opted_in(self):
        self.account.log_bodies = True
        self.deliver(self.message("Your code is 4821"))
        sms_lines = [m for path, m in self.logged if path == sip_keepalive.SMS_LOG_PATH]
        self.assertIn("4821", " ".join(sms_lines))

    def test_options_answered_200(self):
        reply = self.deliver(
            f"OPTIONS sip:{USERNAME}@voip.ms SIP/2.0" + CRLF +
            f"Via: SIP/2.0/UDP 127.0.0.1:{self.server.port};branch=z9hG4bKopt" + CRLF +
            "From: <sip:ping@voip.ms>;tag=p" + CRLF +
            f"To: <sip:{USERNAME}@voip.ms>" + CRLF +
            "Call-ID: opt-1" + CRLF + "CSeq: 1 OPTIONS" + CRLF + CRLF)
        self.assertIn("200 OK", reply.split(CRLF)[0])

    def test_invite_declined_480(self):
        """480 lets VoIP.ms DID failover apply, as it does when unregistered."""
        reply = self.deliver(
            f"INVITE sip:{USERNAME}@voip.ms SIP/2.0" + CRLF +
            f"Via: SIP/2.0/UDP 127.0.0.1:{self.server.port};branch=z9hG4bKinv" + CRLF +
            "From: <sip:15559876543@voip.ms>;tag=c" + CRLF +
            f"To: <sip:{USERNAME}@voip.ms>" + CRLF +
            "Call-ID: inv-1" + CRLF + "CSeq: 1 INVITE" + CRLF + CRLF)
        self.assertIn("480", reply.split(CRLF)[0])


class ThreadDeathTests(unittest.TestCase):
    """Regression: a dead worker must not keep reporting itself registered.

    This is the worst failure mode available - the dashboard shows green while
    inbound SMS is being rejected.
    """

    def test_mark_thread_dead_clears_registration(self):
        account = make_account("127.0.0.1", 5060, dids=["5551234567"])
        with account.lock:
            account.registered = True
            account.registered_until = __import__("time").time() + 300
        account.mark_thread_dead()
        snap = account.snapshot()
        self.assertFalse(snap["registered"])
        self.assertEqual(snap["seconds_until_expiry"], 0)
        self.assertIn("worker thread died", snap["last_error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
