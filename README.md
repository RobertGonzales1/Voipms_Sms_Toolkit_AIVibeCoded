# VoIP.ms SMS Toolkit

One script to run:

```bash
powershell -ExecutionPolicy Bypass -File ".\start.ps1"
```

That starts the keepalive service **and** shows live registration status for every
number in the same window. Ctrl+C stops the service cleanly.

Underneath:

- **`start.ps1`** — the only thing you run. Owns the daemon and the display.
- **`sip_keepalive.py`** — the service itself: holds subaccounts SIP-registered so
  VoIP.ms accepts inbound SMS. This is what fixes the rejections.
- **`voipms_watch.py`** — optional. Checks the non-SIP failure modes (balance,
  per-DID SMS settings drift).

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

**Prefer environment variables for the passwords.** A SIP password permits
outbound calling at your expense, and this folder is cloud-synced — `.gitignore`
keeps `sip_config.json` out of the repo but not out of OneDrive. Set
`VOIPMS_SIP_PASSWORD_<LABEL>` (uppercased, non-alphanumerics as underscores, so
label `main-line` reads `VOIPMS_SIP_PASSWORD_MAIN_LINE`) and omit `password`
entirely:

```bash
powershell -Command "[Environment]::SetEnvironmentVariable('VOIPMS_SIP_PASSWORD_MAIN_LINE','your-sip-password','User')"
```

The daemon warns at startup if it finds passwords in a config file inside a
synced folder.

- **`did`** — the phone number this subaccount receives for. Display only: it
  labels the row in the dashboard. Use `"dids": ["...", "..."]` if one subaccount
  covers several numbers.

```json
{
  "defaults": {
    "server": "chicago.voip.ms",
    "expires": 300,
    "nat_keepalive": 30
  },
  "accounts": [
    { "label": "main",     "did": "5551234567", "account": "123456_main", "password": "..." },
    { "label": "business", "did": "5559876543", "account": "123456_biz",  "password": "..." }
  ]
}
```

### Finding which subaccount each number uses

If you are not sure of the pairing, this prints every DID with the subaccount
fields attached to it, plus a ready-to-paste `accounts` skeleton:

```bash
python voipms_watch.py map
```

It needs the API credentials from Part 2 below.

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

### Run it permanently, with no window

Optional. If you would rather not keep a window open, install it as a Scheduled
Task instead:

```bash
powershell -ExecutionPolicy Bypass -File .\install-keepalive-task.ps1
```

Starts at logon, restarts automatically if it dies, no time limit, keeps running
on battery. Add `-AtStartup` to trigger at boot instead of logon — that needs the
task to run as SYSTEM or with stored credentials, since no one is signed in yet.

You can still run `start.ps1` any time to watch it; it will attach to the task's
daemon rather than starting a competing one.

### Run it

```bash
powershell -ExecutionPolicy Bypass -File ".\start.ps1"
```

```
  VoIP.ms SIP Registration                           2026-08-29 14:22:45
  ============================================================================
  daemon: RUNNING  (pid 24188, updated 3s ago)

  PHONE NUMBER      LABEL         SUBACCOUNT        STATUS          RENEWS  SMS
  ----------------------------------------------------------------------------
  (555) 123-4567    main          123456_main       REGISTERED      247s    3
  (555) 987-6543    business      123456_biz        REGISTERED      190s    0
  (555) 222-3333
  (555) 444-5555    alerts        123456_alerts     NOT REGISTERED  --      0
      authentication rejected (403) - check the subaccount password
  ============================================================================
  3 of 4 registered                       Ctrl+C to stop the daemon and exit
```

Green means registered, red means not, and a failing account prints its reason
underneath. The `daemon:` line tracks the service itself — it turns red if the
process dies or stops writing status, which is exactly the state where your
registrations are quietly expiring.

Ctrl+C asks the service to shut down cleanly so each subaccount deregisters,
rather than leaving stale registrations to time out. Refresh rate is
`-RefreshSeconds 5` if you want it calmer.

**Leave this window open.** Registrations last only as long as the service runs.

### If a service is already running

`start.ps1` checks first. If it finds a live daemon — for example the Scheduled
Task below — it attaches and displays only, and does not start a second one or
stop the existing one when you close it. That matters: registering the same
subaccount twice is what VoIP.ms warns breaks SMS delivery.

For a plain one-shot check with no window:

```bash
python sip_keepalive.py --status
```

### Files it writes

| File | |
| --- | --- |
| `sip_status.json` | Current per-account state; written on change, plus a 15s heartbeat |
| `sip_keepalive.log` | Registration events and errors; rotates at 2 MB |
| `sip_messages.log` | Sender and timestamp for every inbound SMS accepted |

`sip_messages.log` is a useful backstop: if a text is recorded there but never
reached your phone, the problem is downstream of VoIP.ms, not registration.

**Message bodies are not logged by default.** Most of what arrives on these
numbers is 2FA codes and password resets, and this folder syncs to the cloud.
Sender and timestamp are enough to answer "did it reach VoIP.ms?" without keeping
the secret on disk. Set `"log_message_bodies": true` under `defaults` if you
need the text for debugging.

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
| `map` | Shows which subaccount each DID uses, with a `sip_config.json` skeleton |
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

## Known limitations

- **No transport security.** SIP runs over plain UDP here. Digest auth protects
  the *password*, not the *message*, so inbound SMS bodies cross the internet in
  cleartext. VoIP.ms offers TLS on some POPs; supporting it would mean TCP framing
  and certificate validation, and is not implemented.
- **A hard kill orphans the daemon.** Ctrl+C shuts it down cleanly, but
  `taskkill /F` or killing the PowerShell job skips that path and leaves the
  Python process running. Recover with `python sip_keepalive.py --status` to find
  the pid, then stop it — or just create the `sip_stop.flag` file, which the
  daemon polls once a second.
- **The machine has to stay awake.** See above.

## Tests

```bash
python test_sip.py
```

```bash
python test_logic.py
```

Both are `unittest` suites (stdlib, no dependencies) and run fully offline —
`test_sip.py` binds only to `127.0.0.1`. It stands up a fake SIP registrar and
exercises the real digest handshake, including the RFC 2617 reference vector.

Run one class while working on it:

```bash
python -m unittest test_sip.ExpiryTests -v
```

## Repository

Private: <https://github.com/RobertGonzales1/voipms-sms-toolkit>

`.gitignore` excludes `config.json`, `sip_config.json`, `sip_status.json`,
`baseline.json` and all logs — so passwords and SMS content stay local. Keep it
that way if you add files: `sip_messages.log` contains message bodies.
