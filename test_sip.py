#!/usr/bin/env python3
"""Tests for the SIP keepalive. No VoIP.ms account or network needed.

Spins up a fake SIP server on 127.0.0.1 that issues a digest challenge, verifies
our Authorization header the way a real registrar would, and then pushes an
inbound MESSAGE / OPTIONS / INVITE at us to confirm we answer correctly.

Run:  python test_sip.py
"""

import queue
import socket
import sys
import threading
import time

from sip_keepalive import (
    CRLF,
    SipAccount,
    _md5,
    build_authorization,
    parse_auth_header,
    parse_sip,
)

FAILURES = []
REALM = "voip.ms"
NONCE = "3f1a9c2b7e4d6a8f"
PASSWORD = "s3cr3t-sip-pw"
USERNAME = "123456_test"


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# --------------------------------------------------------------------------
print("digest auth")

# RFC 2617 section 3.5 reference vector - proves the hash chain is correct.
ha1 = _md5("Mufasa:testrealm@host.com:Circle Of Life")
ha2 = _md5("GET:/dir/index.html")
rfc = _md5(f"{ha1}:dcd98b7102dd2f0e8b11d0f600bfb0c093:00000001:0a4f113b:auth:{ha2}")
check("RFC 2617 qop=auth vector", rfc == "6629fae49393a05397450978507c4ef1", rfc)

ha1b = _md5("Mufasa:testrealm@host.com:Circle Of Life")
ha2b = _md5("GET:/dir/index.html")
legacy = _md5(f"{ha1b}:dcd98b7102dd2f0e8b11d0f600bfb0c093:{ha2b}")
check("legacy no-qop vector", legacy == "670fd8c2df070c60b045671b8b24ff02", legacy)

challenge = parse_auth_header(
    f'Digest realm="{REALM}", nonce="{NONCE}", algorithm=MD5, qop="auth"'
)
check("parse realm", challenge.get("realm") == REALM, challenge)
check("parse nonce", challenge.get("nonce") == NONCE, challenge)
check("parse qop", challenge.get("qop") == "auth", challenge)
check("parse algorithm", challenge.get("algorithm") == "MD5", challenge)

quoted = parse_auth_header('Digest realm="a,b", nonce="x=y", qop="auth"')
check("commas inside quotes survive", quoted.get("realm") == "a,b", quoted)
check("equals inside quotes survive", quoted.get("nonce") == "x=y", quoted)

no_qop = parse_auth_header(f'Digest realm="{REALM}", nonce="{NONCE}"')
auth_value = build_authorization(USERNAME, PASSWORD, no_qop, "REGISTER", "sip:voip.ms", 1)
parsed = parse_auth_header(auth_value)
expected = _md5(f'{_md5(f"{USERNAME}:{REALM}:{PASSWORD}")}:{NONCE}:{_md5("REGISTER:sip:voip.ms")}')
check("no-qop response is correct", parsed.get("response") == expected, parsed)
check("no-qop omits nc", "nc" not in parsed, parsed)


# --------------------------------------------------------------------------
print("\nSIP parsing")

msg = parse_sip(
    "SIP/2.0 401 Unauthorized" + CRLF +
    "Via: SIP/2.0/UDP 10.0.0.5:5060" + CRLF +
    'WWW-Authenticate: Digest realm="voip.ms", nonce="abc"' + CRLF + CRLF
)
check("status parsed", msg["status"] == 401, msg["status"])
check("method is None on a response", msg["method"] is None, msg["method"])
check("header lowercased", "www-authenticate" in msg["headers"], list(msg["headers"]))

req = parse_sip(
    "MESSAGE sip:123456_test@voip.ms SIP/2.0" + CRLF +
    "Content-Type: text/plain" + CRLF + CRLF +
    "hello world"
)
check("method parsed", req["method"] == "MESSAGE", req["method"])
check("status is None on a request", req["status"] is None, req["status"])
check("body extracted", req["body"] == "hello world", repr(req["body"]))


# --------------------------------------------------------------------------
print("\nREGISTER handshake against a fake registrar")


class FakeRegistrar(threading.Thread):
    """Minimal SIP registrar: challenges once, then validates the digest."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.client_addr = None
        self.digest_ok = None
        self.expires_seen = None
        self.contact_seen = None
        self.deregistered = False
        self.stop = threading.Event()
        # Anything that is not a REGISTER goes here, so the reader thread and
        # recv() never race each other for the same datagram.
        self.inbox = queue.Queue()

    def run(self):
        self.sock.settimeout(0.5)
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

            via = msg["headers"].get("via", "")
            cseq = msg["headers"].get("cseq", "1 REGISTER")
            common = (
                f"Via: {via}" + CRLF +
                f"From: {msg['headers'].get('from','')}" + CRLF +
                f"To: {msg['headers'].get('to','')};tag=srv" + CRLF +
                f"Call-ID: {msg['headers'].get('call-id','')}" + CRLF +
                f"CSeq: {cseq}" + CRLF
            )

            auth = msg["headers"].get("authorization")
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
            self.expires_seen = msg["headers"].get("expires")
            self.contact_seen = msg["headers"].get("contact")
            if self.expires_seen == "0":
                self.deregistered = True

            self.sock.sendto((
                "SIP/2.0 200 OK" + CRLF + common +
                f"Contact: {self.contact_seen};expires=300" + CRLF +
                "Content-Length: 0" + CRLF + CRLF
            ).encode(), addr)

    def _verify(self, header):
        p = parse_auth_header(header)
        ha1 = _md5(f"{p.get('username')}:{REALM}:{PASSWORD}")
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


server = FakeRegistrar()
server.start()

account = SipAccount(
    {"account": USERNAME, "password": PASSWORD, "label": "test"},
    {"server": "127.0.0.1", "sip_port": server.port, "expires": 300},
)
account.open_socket()

ok = account.do_register(300)
check("REGISTER succeeded", ok is True, account.last_error)
check("server accepted our digest", server.digest_ok is True, server.digest_ok)
check("account marked registered", account.registered is True)
check("requested expires sent", server.expires_seen == "300", server.expires_seen)
check("Contact carries our local port",
      server.contact_seen and f":{account.local_port}" in server.contact_seen,
      server.contact_seen)
check("expiry honoured from server Contact",
      account.registered_until - time.time() > 250,
      account.registered_until - time.time())


# --------------------------------------------------------------------------
print("\ninbound request handling")

server.send_to_client(
    f"MESSAGE sip:{USERNAME}@voip.ms SIP/2.0" + CRLF +
    f"Via: SIP/2.0/UDP 127.0.0.1:{server.port};branch=z9hG4bKtest" + CRLF +
    "From: <sip:15551234567@voip.ms>;tag=abc" + CRLF +
    f"To: <sip:{USERNAME}@voip.ms>" + CRLF +
    "Call-ID: msg-test-1" + CRLF +
    "CSeq: 1 MESSAGE" + CRLF +
    "Content-Type: text/plain" + CRLF +
    "Content-Length: 17" + CRLF + CRLF +
    "Your code is 4821"
)
time.sleep(0.2)
data, _ = account.sock.recvfrom(65535)
incoming = parse_sip(data.decode())
account.handle_request(incoming)

reply = server.recv()
check("MESSAGE answered", reply is not None and "200 OK" in reply.split(CRLF)[0], reply)
check("reply echoes Call-ID", reply and "msg-test-1" in reply)
check("reply carries a To tag", reply and ";tag=" in reply.split("To:")[1].split(CRLF)[0], reply)
check("message counted", account.messages_received == 1, account.messages_received)

server.send_to_client(
    f"OPTIONS sip:{USERNAME}@voip.ms SIP/2.0" + CRLF +
    f"Via: SIP/2.0/UDP 127.0.0.1:{server.port};branch=z9hG4bKopt" + CRLF +
    "From: <sip:ping@voip.ms>;tag=p" + CRLF +
    f"To: <sip:{USERNAME}@voip.ms>" + CRLF +
    "Call-ID: opt-test-1" + CRLF +
    "CSeq: 1 OPTIONS" + CRLF + CRLF
)
time.sleep(0.2)
data, _ = account.sock.recvfrom(65535)
account.handle_request(parse_sip(data.decode()))
reply = server.recv()
check("OPTIONS answered 200", reply is not None and "200 OK" in reply.split(CRLF)[0], reply)

server.send_to_client(
    f"INVITE sip:{USERNAME}@voip.ms SIP/2.0" + CRLF +
    f"Via: SIP/2.0/UDP 127.0.0.1:{server.port};branch=z9hG4bKinv" + CRLF +
    "From: <sip:15559876543@voip.ms>;tag=c" + CRLF +
    f"To: <sip:{USERNAME}@voip.ms>" + CRLF +
    "Call-ID: inv-test-1" + CRLF +
    "CSeq: 1 INVITE" + CRLF + CRLF
)
time.sleep(0.2)
data, _ = account.sock.recvfrom(65535)
account.handle_request(parse_sip(data.decode()))
reply = server.recv()
check("INVITE declined 480 (lets DID failover apply)",
      reply is not None and "480" in reply.split(CRLF)[0], reply)


# --------------------------------------------------------------------------
print("\nderegistration")

account.do_register(0)
check("deregister sent Expires: 0", server.deregistered is True, server.expires_seen)
check("account marked unregistered", account.registered is False)

account.close_socket()
server.stop.set()


# --------------------------------------------------------------------------
print("\nbad password is reported, not retried blindly")

server2 = FakeRegistrar()
server2.start()
bad = SipAccount(
    {"account": USERNAME, "password": "wrong-password", "label": "bad"},
    {"server": "127.0.0.1", "sip_port": server2.port, "expires": 300},
)
bad.open_socket()
bad.do_register(300)
check("wrong password fails the digest check", server2.digest_ok is False, server2.digest_ok)
bad.close_socket()
server2.stop.set()


print()
if FAILURES:
    print(f"{len(FAILURES)} test(s) FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("All tests passed.")
