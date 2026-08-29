# VoIP.ms SMS Toolkit

Two programs that work together:

1. **`sip_keepalive.py`** — holds your subaccounts continuously SIP-registered so
   VoIP.ms accepts inbound SMS. This is the one that fixes the rejections.
2. **`voipms_watch.py`** — watches everything else that can silently break
   delivery (balance, per-DID SMS settings, registration status).

Stdlib Python only. No `pip install`.

## Why registration matters

VoIP.ms will only deliver an inbound SMS to a subaccount that is **registered at
one of their POPs**. When nothing is registered, the delivery attempt fails and
that failure propagates back upstream — the sending company sees a rejection,
which is why messages never arrive rather than arriving late.

So the fix is genuinely to keep a registration alive. That is what
`sip_keepalive.py` does.

Two things it does beyond a naive registrar, both of which matter:

- **Answers `MESSAGE` with `200 OK`.** Being registered is not enough on its own —
  something has to *accept* the message. A registration with nothing behind it
  can still produce failed deliveries.
- **Sends a NAT keepalive** every 30s. Without it your router closes the UDP
  pinhole, and VoIP.ms's reply cannot reach you even though the portal still shows
  you as registered. That mismatch is the classic cause of "it says registered but
  texts still don't come."

### Calls are unaffected

Your DIDs route calls to VoIP.ms voicemail, not to these subaccounts, so nothing
will ever send an `INVITE` to these registrations. As insurance the daemon still
declines `INVITE` with `480 Temporarily Unavailable` (configurable via
`invite_response`), which is the response that lets normal DID failover apply.

### Do not double-register

VoIP.ms warns against registering the same subaccount on more than one softphone
or app at a time. If you later put one of these subaccounts into a phone app, take
it out of `sip_config.json` first — otherwise the daemon and the app will fight
over the registration.

---

## Part 1: SIP keepalive

### Setup

Copy the example config and fill it in:

```bash
cd "C:\Users\RGonz\OneDrive\AI Projects\Claude\voip_ms" && copy sip_config.example.json sip_config.json
```

For each subaccount you need:

- **`account`** — the full subaccount name, e.g. `123456_main`
- **`password`** — the **subaccount SIP password**. Not your portal password and
  not the API password. Found under *Sub Accounts > Manage Sub Accounts >* pencil icon.
- **`server`** — your POP, e.g. `chicago.voip.ms`. Use the one closest to you;
  it is shown on each subaccount's page.

```json
{
  "defaults": {
    "server": "chicago.voip.ms",
    "expires": 300,
    "nat_keepalive": 30
  },
  "accounts": [
    { "label": "main",     "account": "123456_main", "password": "..." },
    { "label": "business", "account": "123456_biz",  "password": "..." }
  ]
}
```

### Verify one account before committing

```bash
python sip_keepalive.py --check
```

This registers each account once, reports, then cleanly deregisters. Start with a
single account in the config, confirm it shows registered in the VoIP.ms portal
while the check runs, then add the rest.

If it fails, `--verbose` prints the full SIP exchange:

```bash
python sip_keepalive.py --check --verbose
```

### Run it

```bash
python sip_keepalive.py
```

Leave it running. Registration lasts only as long as something refreshes it.

### Run it permanently

```bash
powershell -ExecutionPolicy Bypass -File .\install-keepalive-task.ps1
```

Starts at logon, restarts automatically if it dies, no time limit, keeps running
on battery. Add `-AtStartup` to trigger at boot instead of logon — that needs the
task to run as SYSTEM or with stored credentials, since no one is signed in yet.

```bash
powershell -Command "Start-ScheduledTask -TaskName 'VoIPms SIP Keepalive'"
```

Check on it any time:

```bash
python sip_keepalive.py --status
```

### Files it writes

| File | |
| --- | --- |
| `sip_status.json` | Current per-account state, rewritten every 15s |
| `sip_keepalive.log` | Registration events and errors |
| `sip_messages.log` | Every inbound SMS the daemon accepted, with sender and body |

`sip_messages.log` is a useful backstop: if a text is in there but never reached
your phone, the problem is downstream of VoIP.ms, not registration.

### This machine has to stay on

The daemon only holds registrations while it runs. A desktop that sleeps or a
laptop that travels will drop them. If that is a problem, run it on something
always-on — a mini PC, a NAS with Python, or a small VPS. It is stdlib-only, so it
will run anywhere Python 3.8+ does.

---

## Part 2: Config watchdog

Catches the other failure modes: a balance that hit zero and suspended service, or
per-DID SMS settings that silently changed.

### Setup

Enable the API in the portal: **Main Menu > SOAP/REST API** — set an API password
(separate from your portal password), enable API, and allowlist this machine's
public IP.

Credentials belong in environment variables, not `config.json`. This folder is
inside OneDrive, so a config file holding your API password would sync to the cloud:

```bash
powershell -Command "[Environment]::SetEnvironmentVariable('VOIPMS_API_USERNAME','rvg2151@gmail.com','User'); [Environment]::SetEnvironmentVariable('VOIPMS_API_PASSWORD','your-api-password','User')"
```

Open a new terminal, then verify and snapshot a known-good config:

```bash
python voipms_watch.py test
```

```bash
python voipms_watch.py baseline
```

### Commands

| Command | |
| --- | --- |
| `test` | Probes credentials and which API methods your account exposes |
| `baseline` | Snapshots per-DID SMS settings to `baseline.json` |
| `check` | Balance, route sanity, drift vs baseline, registration, keepalive health |
| `repair` | Re-applies the baseline to drifted DIDs, then re-reads to verify it stuck |

Flags: `--json`, and `--dry-run` on `repair`. Exit codes: `0` clear, `1` warnings,
`2` critical, `3` tool/API error.

`check` reads `sip_status.json`, so it catches a dead keepalive daemon even when
the portal still shows a not-yet-expired registration.

### Schedule it

```bash
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
```

Runs `check` every 6 hours, notifies only on failure. `-IntervalHours 12` to
change cadence, `-Uninstall` to remove.

---

## If texts still go missing while everything shows green

Then registration is not the remaining problem, and the likely cause is
**carrier-side A2P filtering** — mobile carriers dropping texts relayed from a VoIP
DID to a cell number, which hits 2FA codes hardest. That is upstream of VoIP.ms
and cannot be fixed from here. The workaround is to stop relying on the
cell-number hop and read those messages from email, the VoIP.ms portal, or
`sip_messages.log`.

Check `sip_messages.log` first — it tells you which side of the line the message
died on.

## Tests

```bash
python test_sip.py
```

```bash
python test_logic.py
```

Both run fully offline. `test_sip.py` stands up a fake SIP registrar on localhost
and exercises the real digest handshake, including the RFC 2617 reference vector.
