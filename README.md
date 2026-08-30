# VoIP.ms SIP Keepalive

Keeps your VoIP.ms subaccounts continuously SIP-registered, so inbound SMS is
accepted instead of rejected — and shows you live whether it's working.

One script to run:

```bash
powershell -ExecutionPolicy Bypass -File ".\start.ps1"
```

Stdlib Python only. No `pip install`, no admin rights.

## Why registration matters

VoIP.ms only delivers an inbound SMS to a subaccount that is **registered at one
of their POPs**. When nothing is registered, the delivery attempt fails and that
failure propagates back upstream — the sending company sees a rejection, which is
why messages never arrive rather than arriving late.

Two things this does beyond a naive registrar, both of which matter:

- **Answers `MESSAGE` with `200 OK`.** Being registered is not enough on its own —
  something has to *accept* the message. A registration with nothing behind it can
  still produce failed deliveries.
- **Sends a NAT keepalive** every 30s. Without it your router closes the UDP
  pinhole, and VoIP.ms's reply cannot reach you even though the portal still shows
  you as registered. That mismatch is the classic cause of "it says registered but
  texts still don't come."

### Calls are unaffected

DIDs that route calls to VoIP.ms voicemail never send an `INVITE` to these
subaccounts. As insurance the daemon declines `INVITE` with `480 Temporarily
Unavailable` (configurable via `invite_response`), which is the response that lets
normal DID failover apply.

### Do not double-register

VoIP.ms warns against registering the same subaccount on more than one softphone
or app at a time. If you later put one of these subaccounts into a phone app, take
it out of `sip_config.json` first — otherwise the daemon and the app will fight
over the registration.

---

## Setup

### 1. Create your config

```bash
copy sip_config.example.json sip_config.json
```

### 2. Fill it in from the portal

**Sub Accounts → Manage Sub Accounts**, then the pencil icon on each. You need:

- **`account`** — the subaccount name, e.g. `123456_main`
- **`password`** — the **subaccount SIP password**. Not your portal password.
- **`server`** — your POP, e.g. `chicago.voip.ms`, shown on the same page
- **`did`** — the phone number. Display only: it labels the row on screen. Use
  `"dids": ["...", "..."]` if one subaccount covers several numbers.

```json
{
  "defaults": { "server": "chicago.voip.ms", "expires": 300, "nat_keepalive": 30 },
  "accounts": [
    { "label": "main",     "did": "5551234567", "account": "123456_main", "password": "..." },
    { "label": "business", "did": "5559876543", "account": "123456_biz",  "password": "..." }
  ]
}
```

**Consider environment variables for the passwords.** A SIP password permits
outbound calling at your expense. `.gitignore` keeps `sip_config.json` out of the
repo, but if the folder sits in OneDrive or Dropbox the file still syncs to the
provider — the daemon warns at startup when it detects that. On a machine with no
cloud sync (a VM, say) a local config file is a perfectly reasonable choice. Set
`VOIPMS_SIP_PASSWORD_<LABEL>` (uppercased, non-alphanumerics as underscores, so
label `main-line` reads `VOIPMS_SIP_PASSWORD_MAIN_LINE`) and omit `password`:

```bash
powershell -Command "[Environment]::SetEnvironmentVariable('VOIPMS_SIP_PASSWORD_MAIN','your-sip-password','User')"
```

The daemon warns at startup if it finds passwords in a config file inside a synced
folder.

### 3. Test before committing to it

```bash
python sip_keepalive.py --check
```

Registers each account once, reports, then cleanly deregisters — it does not stay
resident. Every line should read `[ok]`.

**Start with one account**, confirm it flips to registered in the portal while the
check runs, then add the rest. If something fails, `--check --verbose` prints the
full SIP exchange.

### 4. Run it

```bash
powershell -ExecutionPolicy Bypass -File ".\start.ps1"
```

```
  VoIP.ms SIP Keepalive                              2026-08-29 14:22:45
  ============================================================================
  daemon: RUNNING  (pid 24188, started by this window, updated 3s ago)

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

Green is registered, red is not, and a failing account prints its reason
underneath. The `daemon:` line tracks the service itself — it turns red if the
process dies or stops writing status, which is exactly the state where your
registrations are quietly expiring.

Ctrl+C shuts down cleanly so each subaccount deregisters. `-RefreshSeconds 5` if
you want it calmer.

**Leave the window open.** Registrations last only as long as the service runs.

---

## Deploying to another machine

A VM is a better host than a desktop: it does not sleep, and registrations only
hold while the service runs.

> **Run it in exactly one place.** Two hosts registering the same subaccount will
> fight over it, which is the double-registration VoIP.ms warns breaks SMS
> delivery. Before starting it somewhere new, stop it wherever it was:
> `powershell -File .\install-keepalive-task.ps1 -Uninstall`

### 1. Install Python

Get Python 3.8+ from python.org. Two boxes matter on the installer:

- **Add python.exe to PATH**
- **Install for all users** — required if you intend to run at boot as SYSTEM,
  because SYSTEM cannot reach a per-user install. The installer warns you if you
  get this wrong.

### 2. Get the code

The repo is private, so the clone needs credentials. Easiest is the GitHub CLI:

```bash
winget install --id GitHub.cli
```

```bash
gh auth login
```

```bash
gh repo clone RobertGonzales1/Voipms_Sms_Toolkit_AIVibeCoded C:\voipms
```

No GitHub on the VM? Copying the folder over works just as well — it is seven
files and nothing is compiled. Do **not** copy `sip_config.json` across if you can
avoid it; create it fresh on the VM so the password is not sitting in a second
place.

### 3. Configure

`sip_config.json` is deliberately not in the repo, so create it on the VM:

```bash
cd C:\voipms && copy sip_config.example.json sip_config.json
```

Fill it in as in Setup above. **There is no OneDrive on the VM**, so the
cloud-sync objection to putting passwords in the file does not apply — a local
`sip_config.json` on a machine only you reach is a reasonable choice, and it is
the simpler option if you plan to run at boot.

If you would still rather use environment variables, mind the scope:

| Task mode | Runs as | Variable scope needed |
| --- | --- | --- |
| default (logon) | you | `User` or `Machine` |
| `-AtStartup` | SYSTEM | **`Machine` only** |

```bash
powershell -Command "[Environment]::SetEnvironmentVariable('VOIPMS_SIP_PASSWORD_MAIN','your-sip-password','Machine')"
```

Setting a Machine-scoped variable needs an elevated prompt.

### 4. Verify, then install as a boot service

```bash
cd C:\voipms && python sip_keepalive.py --check
```

Once every line reads `[ok]`, from an **elevated** PowerShell:

```bash
cd C:\voipms && powershell -ExecutionPolicy Bypass -File .\install-keepalive-task.ps1 -AtStartup
```

`-AtStartup` registers the task to run as SYSTEM at boot, so it comes back after a
reboot with nobody logged in. Without that flag it runs at logon instead, which is
fine for a VM you keep signed into.

```bash
powershell -Command "Start-ScheduledTask -TaskName 'VoIPms SIP Keepalive'"
```

```bash
cd C:\voipms && python sip_keepalive.py --status
```

### 5. Confirm it survives a reboot

Worth doing once — it is the whole point of `-AtStartup`. Reboot the VM, wait a
minute, then run `--status` again. Registrations should be live with nobody logged
in.

---

## Running it permanently, with no window

Optional. If you would rather not keep a window open:

```bash
powershell -ExecutionPolicy Bypass -File .\install-keepalive-task.ps1
```

Starts at logon, restarts automatically if it dies, no time limit, keeps running
on battery. `-AtStartup` triggers at boot instead of logon — that needs the task
to run as SYSTEM or with stored credentials, since no one is signed in yet.
`-Uninstall` removes it.

You can still run `start.ps1` any time to watch it: it detects the existing daemon
and attaches, rather than starting a competing one.

For a plain one-shot check with no window:

```bash
python sip_keepalive.py --status
```

---

## Files it writes

| File | |
| --- | --- |
| `sip_status.json` | Per-account state; written on change, plus a 15s heartbeat |
| `sip_keepalive.log` | Registration events and errors; rotates at 2 MB |
| `sip_messages.log` | Sender and timestamp for every inbound SMS accepted |

`sip_messages.log` is a useful backstop: if a text is recorded there but never
reached your phone, the problem is downstream of VoIP.ms, not registration.

**Message bodies are not logged by default.** Most of what arrives on these
numbers is 2FA codes and password resets, and this folder syncs to the cloud.
Sender and timestamp are enough to answer "did it reach VoIP.ms?" without keeping
the secret on disk. Set `"log_message_bodies": true` under `defaults` if you need
the text for debugging.

## Known limitations

- **The machine has to stay awake.** Registrations only hold while the service
  runs, so a PC that sleeps will drop them — which looks exactly like the original
  problem. If that's an issue, run it on something always-on; it's stdlib-only and
  runs anywhere Python 3.8+ does.
- **No transport security.** SIP runs over plain UDP. Digest auth protects the
  *password*, not the *message*, so inbound SMS bodies cross the internet in
  cleartext. VoIP.ms offers TLS on some POPs; supporting it would mean TCP framing
  and certificate validation, and is not implemented.
- **A hard kill orphans the daemon.** Ctrl+C shuts down cleanly, but `taskkill /F`
  or killing the PowerShell job skips that path. Recover with
  `python sip_keepalive.py --status` to find the pid, or create a `sip_stop.flag`
  file, which the daemon polls once a second.

## If texts still go missing while everything shows green

Then registration is not the remaining problem, and the likely cause is
**carrier-side A2P filtering** — mobile carriers dropping texts relayed from a VoIP
DID to a cell number, which hits 2FA codes hardest. That is upstream of VoIP.ms and
cannot be fixed from here. Check `sip_messages.log` first: it tells you which side
of the line the message died on.

## Tests

```bash
python test_sip.py
```

A `unittest` suite (stdlib, no dependencies), fully offline — it binds only to
`127.0.0.1`. It stands up a fake SIP registrar and exercises the real digest
handshake, including the RFC 2617 reference vector.

Run one class while working on it:

```bash
python -m unittest test_sip.ExpiryTests -v
```

## Repository

Private: <https://github.com/RobertGonzales1/Voipms_Sms_Toolkit_AIVibeCoded>

`.gitignore` excludes `sip_config.json`, `sip_status.json` and all logs, so
passwords and SMS metadata stay local.

An earlier API-based watchdog (`voipms_watch.py`) that checked account balance and
per-DID SMS settings was removed in favour of keeping this to one job. It is still
in git history if it's ever wanted back.
