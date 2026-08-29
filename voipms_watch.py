#!/usr/bin/env python3
"""
voipms_watch.py - VoIP.ms SMS delivery watchdog.

Monitors the things that actually break server-side SMS forwarding:
  * account balance (a zero/negative balance suspends service)
  * per-DID SMS settings drift (forwarding silently turned off or blanked)
  * subaccount registration status (informational)
  * inbound SMS freshness (early warning that a DID went quiet)

Stdlib only - no pip install required.

Usage:
    python voipms_watch.py test                 # probe credentials + API methods
    python voipms_watch.py baseline             # snapshot current good config
    python voipms_watch.py check                # compare live config vs baseline
    python voipms_watch.py check --json         # machine-readable output
    python voipms_watch.py repair               # re-apply baseline, then verify
    python voipms_watch.py repair --dry-run     # show what repair would change

Exit codes:  0 = all clear   1 = warnings   2 = critical   3 = tool/API error
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://voip.ms/api/v1/rest.php"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
BASELINE_PATH = os.path.join(HERE, "baseline.json")
LOG_PATH = os.path.join(HERE, "watch.log")
SIP_STATUS_PATH = os.path.join(HERE, "sip_status.json")

# The keepalive daemon rewrites its status every 15s; allow generous slack.
KEEPALIVE_STALE_SECONDS = 180

# VoIP.ms enforces >= 60s between setSMS enable/disable calls on the same DID.
SETSMS_COOLDOWN_SECONDS = 61

# SMS settings we track for drift. Maps the getDIDsInfo field -> setSMS param.
SMS_FIELDS = {
    "sms_enabled": "enable",
    "sms_email_enabled": "email_enabled",
    "sms_email": "email_address",
    "sms_forward_enabled": "sms_forward_enable",
    "sms_forward": "sms_forward",
    "sms_url_callback_enabled": "url_callback_enable",
    "sms_url_callback": "url_callback",
}

CRITICAL, WARNING, INFO = "CRITICAL", "WARNING", "INFO"


class VoipmsError(RuntimeError):
    """An API call failed or returned a non-success status."""


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config() -> dict:
    """Credentials from env vars first, then config.json. Env wins."""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)

    cfg["api_username"] = os.environ.get("VOIPMS_API_USERNAME") or cfg.get("api_username", "")
    cfg["api_password"] = os.environ.get("VOIPMS_API_PASSWORD") or cfg.get("api_password", "")

    if not cfg["api_username"] or not cfg["api_password"]:
        raise VoipmsError(
            "Missing credentials.\n"
            "  Set VOIPMS_API_USERNAME / VOIPMS_API_PASSWORD, or fill in config.json.\n"
            "  (Copy config.example.json to config.json first.)"
        )

    cfg.setdefault("min_balance", 5.0)
    cfg.setdefault("stale_inbound_days", 0)   # 0 = disabled
    cfg.setdefault("check_registration", True)
    cfg.setdefault("timeout_seconds", 30)
    return cfg


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------

def api(cfg: dict, method: str, **params) -> dict:
    """Call one REST method. Raises VoipmsError on transport or status failure."""
    query = {
        "api_username": cfg["api_username"],
        "api_password": cfg["api_password"],
        "method": method,
        "content_type": "json",
    }
    for key, value in params.items():
        if value is not None:
            query[key] = str(value)

    url = API_URL + "?" + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(url, timeout=cfg["timeout_seconds"]) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise VoipmsError(f"{method}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise VoipmsError(f"{method}: network error - {exc.reason}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VoipmsError(f"{method}: non-JSON response - {raw[:200]!r}") from exc

    status = payload.get("status")
    if status != "success":
        raise VoipmsError(f"{method}: API returned status={status!r}{_explain(status)}")
    return payload


def _explain(status) -> str:
    """Turn the common VoIP.ms error codes into something actionable."""
    hints = {
        "invalid_credentials": "  -> wrong API password (it is NOT your portal password)",
        "ip_not_enabled": "  -> this machine's public IP is not allowlisted in the portal",
        "api_not_enabled": "  -> enable API access under Main Menu > SOAP/REST API",
        "missing_credentials": "  -> api_username/api_password were not sent",
        "invalid_method": "  -> this VoIP.ms account/plan does not expose that method",
        "invalid_did": "  -> that DID is not on this account",
    }
    return hints.get(status, "")


def api_optional(cfg: dict, method: str, **params):
    """Like api(), but returns None instead of raising. For non-essential probes."""
    try:
        return api(cfg, method, **params)
    except VoipmsError:
        return None


# --------------------------------------------------------------------------
# data fetch
# --------------------------------------------------------------------------

def fetch_dids(cfg: dict) -> dict:
    """Return {did: {sms field: value}} for every DID on the account."""
    payload = api(cfg, "getDIDsInfo")
    dids = {}
    for entry in payload.get("dids", []) or payload.get("data", []):
        number = str(entry.get("did", "")).strip()
        if not number:
            continue
        dids[number] = {field: str(entry.get(field, "")) for field in SMS_FIELDS}
        dids[number]["sms_available"] = str(entry.get("sms_available", ""))
        dids[number]["description"] = str(entry.get("description", ""))
    return dids


def fetch_balance(cfg: dict):
    payload = api_optional(cfg, "getBalance")
    if not payload:
        return None
    data = payload.get("data") or payload
    for key in ("current_balance", "balance"):
        if isinstance(data, dict) and key in data:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                return None
    return None


def fetch_subaccounts(cfg: dict) -> list:
    payload = api_optional(cfg, "getSubAccounts")
    if not payload:
        return []
    rows = payload.get("accounts") or payload.get("data") or []
    return [str(r.get("account", "")).strip() for r in rows if r.get("account")]


def fetch_registration(cfg: dict, account: str):
    """Return True / False / None (None = could not determine)."""
    payload = api_optional(cfg, "getRegistrationStatus", account=account)
    if not payload:
        return None
    value = payload.get("registered")
    if isinstance(value, str):
        return value.lower() == "yes"
    return bool(value) if value is not None else None


def fetch_last_inbound(cfg: dict, did: str, days: int):
    """Most recent received-SMS date for a DID, or None. Window capped at 92 days."""
    days = min(days, 92)
    today = dt.date.today()
    window = {
        "from": (today - dt.timedelta(days=days)).isoformat(),
        "to": today.isoformat(),
    }
    payload = api_optional(cfg, "getSMS", did=did, type=0, limit=1, **window)
    if not payload:
        return None
    rows = payload.get("sms") or payload.get("data") or []
    if not rows:
        return None
    return str(rows[0].get("date", "")) or None


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def compare_to_baseline(live: dict, baseline: dict) -> list:
    """Diff live DID SMS settings against the saved baseline."""
    alerts = []

    for did, expected in baseline.items():
        actual = live.get(did)
        if actual is None:
            alerts.append((CRITICAL, f"DID {did} is in the baseline but NOT on the account any more"))
            continue

        for field in SMS_FIELDS:
            want, got = expected.get(field, ""), actual.get(field, "")
            if want == got:
                continue

            # An enabled->disabled flip, or a forwarding target that got blanked,
            # is the exact failure mode that silently kills forwarding.
            turned_off = want == "1" and got in ("0", "")
            blanked = bool(want) and not got
            severity = CRITICAL if (turned_off or blanked) else WARNING
            alerts.append((
                severity,
                f"DID {did}: {field} changed  expected={want!r}  actual={got!r}",
            ))

    for did in live:
        if did not in baseline:
            alerts.append((INFO, f"DID {did} is on the account but not in the baseline (run 'baseline' to adopt it)"))

    return alerts


def check_delivery_sanity(live: dict) -> list:
    """Catch configs that cannot possibly deliver, baseline or not."""
    alerts = []
    for did, info in live.items():
        if info.get("sms_available") == "0":
            alerts.append((WARNING, f"DID {did}: carrier reports SMS not available on this number"))
            continue
        if info.get("sms_enabled") != "1":
            alerts.append((CRITICAL, f"DID {did}: SMS is DISABLED - nothing will be received"))
            continue

        routes = []
        if info.get("sms_email_enabled") == "1" and info.get("sms_email"):
            routes.append("email")
        if info.get("sms_forward_enabled") == "1" and info.get("sms_forward"):
            routes.append("forward-to-number")
        if info.get("sms_url_callback_enabled") == "1" and info.get("sms_url_callback"):
            routes.append("url-callback")

        if not routes:
            # getDIDsInfo does not expose the SIP-delivery flag, so this is a
            # warning rather than critical: SIP MESSAGE may well be the route.
            alerts.append((
                WARNING,
                f"DID {did}: SMS enabled but no email/forward/callback route is set - "
                "fine if this DID delivers over SIP, otherwise messages only reach the web portal",
            ))
    return alerts


def check_keepalive_status() -> list:
    """Read sip_status.json so a dead keepalive daemon is caught even if the
    portal happens to still show a not-yet-expired registration."""
    if not os.path.exists(SIP_STATUS_PATH):
        return [(INFO, "No sip_status.json - SIP keepalive daemon has not run")]

    try:
        with open(SIP_STATUS_PATH, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        updated = dt.datetime.fromisoformat(payload["updated"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return [(WARNING, "sip_status.json is unreadable")]

    alerts = []
    age = (dt.datetime.now() - updated).total_seconds()
    if age > KEEPALIVE_STALE_SECONDS:
        alerts.append((
            CRITICAL,
            f"SIP keepalive status is {int(age)}s old - the daemon looks DEAD "
            f"(expected updates every 15s)",
        ))

    for acct in payload.get("accounts", []):
        if acct.get("registered"):
            alerts.append((INFO, f"Keepalive {acct.get('label')}: registered, "
                                 f"renews in {acct.get('seconds_until_expiry')}s"))
        else:
            alerts.append((CRITICAL, f"Keepalive {acct.get('label')}: NOT REGISTERED - "
                                     f"{acct.get('last_error') or 'unknown'}"))
    return alerts


def run_check(cfg: dict, baseline: dict) -> list:
    alerts = []

    balance = fetch_balance(cfg)
    if balance is None:
        alerts.append((INFO, "Could not read account balance (getBalance unavailable)"))
    elif balance <= 0:
        alerts.append((CRITICAL, f"Account balance is ${balance:.2f} - service is likely SUSPENDED"))
    elif balance < cfg["min_balance"]:
        alerts.append((WARNING, f"Account balance is ${balance:.2f} (below ${cfg['min_balance']:.2f} threshold)"))
    else:
        alerts.append((INFO, f"Balance ${balance:.2f}"))

    live = fetch_dids(cfg)
    if not live:
        alerts.append((CRITICAL, "No DIDs returned by getDIDsInfo"))
        return alerts

    alerts.extend(check_delivery_sanity(live))
    if baseline:
        alerts.extend(compare_to_baseline(live, baseline))
    else:
        alerts.append((INFO, "No baseline saved yet - run 'baseline' to enable drift detection"))

    if cfg["check_registration"]:
        # VoIP.ms only delivers inbound SMS to a subaccount registered at one of
        # their POPs. An unregistered subaccount means senders get rejections.
        for account in fetch_subaccounts(cfg):
            registered = fetch_registration(cfg, account)
            if registered is True:
                alerts.append((INFO, f"Subaccount {account}: registered"))
            elif registered is False:
                alerts.append((
                    CRITICAL,
                    f"Subaccount {account}: NOT REGISTERED - inbound SMS will be rejected. "
                    "Is sip_keepalive.py running?",
                ))

    alerts.extend(check_keepalive_status())

    stale_days = int(cfg["stale_inbound_days"] or 0)
    if stale_days > 0:
        for did in live:
            last = fetch_last_inbound(cfg, did, stale_days)
            if last is None:
                alerts.append((WARNING, f"DID {did}: no inbound SMS in the last {stale_days} day(s)"))

    return alerts


# --------------------------------------------------------------------------
# repair
# --------------------------------------------------------------------------

def run_repair(cfg: dict, baseline: dict, dry_run: bool) -> list:
    """Re-apply baseline SMS settings to any drifted DID, then verify it stuck."""
    if not baseline:
        raise VoipmsError("No baseline.json - run 'baseline' first while the config is known-good.")

    live = fetch_dids(cfg)
    results = []
    drifted = [
        did for did, expected in baseline.items()
        if did in live and any(expected.get(f, "") != live[did].get(f, "") for f in SMS_FIELDS)
    ]

    if not drifted:
        results.append((INFO, "No drift - nothing to repair"))
        return results

    for index, did in enumerate(drifted):
        expected = baseline[did]
        params = {param: expected.get(field, "") for field, param in SMS_FIELDS.items()}

        if dry_run:
            results.append((INFO, f"[dry-run] would call setSMS for DID {did} with {params}"))
            continue

        if index > 0:
            time.sleep(SETSMS_COOLDOWN_SECONDS)  # respect the per-DID setSMS cooldown

        try:
            api(cfg, "setSMS", did=did, **params)
        except VoipmsError as exc:
            results.append((CRITICAL, f"DID {did}: setSMS failed - {exc}"))
            continue

        # Verify rather than trust: re-read and confirm the values actually took.
        verify = fetch_dids(cfg).get(did, {})
        still_wrong = [f for f in SMS_FIELDS if expected.get(f, "") != verify.get(f, "")]
        if still_wrong:
            results.append((
                CRITICAL,
                f"DID {did}: setSMS accepted but these fields did NOT change: {', '.join(still_wrong)}",
            ))
        else:
            results.append((INFO, f"DID {did}: settings restored from baseline"))

    return results


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def worst_severity(alerts: list) -> str:
    if any(sev == CRITICAL for sev, _ in alerts):
        return CRITICAL
    if any(sev == WARNING for sev, _ in alerts):
        return WARNING
    return INFO


def report(alerts: list, as_json: bool) -> int:
    stamp = dt.datetime.now().replace(microsecond=0).isoformat()
    worst = worst_severity(alerts)

    if as_json:
        print(json.dumps(
            {"timestamp": stamp, "severity": worst,
             "alerts": [{"severity": s, "message": m} for s, m in alerts]},
            indent=2,
        ))
    else:
        icons = {CRITICAL: "[!!]", WARNING: "[! ]", INFO: "[ok]"}
        print(f"VoIP.ms watch - {stamp}")
        print("-" * 62)
        for severity, message in alerts:
            print(f"{icons[severity]} {message}")
        print("-" * 62)
        print(f"Result: {worst}")

    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            for severity, message in alerts:
                if severity != INFO:
                    fh.write(f"{stamp}\t{severity}\t{message}\n")
    except OSError:
        pass  # logging must never mask the real result

    return {CRITICAL: 2, WARNING: 1, INFO: 0}[worst]


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_test(cfg: dict) -> int:
    print("Probing VoIP.ms API...\n")
    probes = ["getBalance", "getDIDsInfo", "getSubAccounts", "getServersInfo"]
    ok = True
    for method in probes:
        try:
            api(cfg, method)
            print(f"  OK        {method}")
        except VoipmsError as exc:
            print(f"  FAILED    {exc}")
            if method in ("getBalance", "getDIDsInfo"):
                ok = False

    accounts = fetch_subaccounts(cfg)
    if accounts:
        print(f"\n  Subaccounts found: {', '.join(accounts)}")
        status = fetch_registration(cfg, accounts[0])
        label = "FAILED" if status is None else "OK    "
        print(f"  {label}    getRegistrationStatus ({accounts[0]} -> registered={status})")

    print("\nCredentials look usable." if ok else "\nCore methods failed - fix the errors above first.")
    return 0 if ok else 3


def cmd_baseline(cfg: dict) -> int:
    live = fetch_dids(cfg)
    if not live:
        raise VoipmsError("getDIDsInfo returned no DIDs - refusing to write an empty baseline")

    snapshot = {did: {f: info.get(f, "") for f in SMS_FIELDS} for did, info in live.items()}
    with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, sort_keys=True)

    print(f"Baseline saved for {len(snapshot)} DID(s) -> {BASELINE_PATH}\n")
    for did, info in sorted(snapshot.items()):
        routes = []
        if info.get("sms_email_enabled") == "1":
            routes.append(f"email->{info.get('sms_email') or '(blank!)'}")
        if info.get("sms_forward_enabled") == "1":
            routes.append(f"fwd->{info.get('sms_forward') or '(blank!)'}")
        if info.get("sms_url_callback_enabled") == "1":
            routes.append("callback")
        state = "SMS on " if info.get("sms_enabled") == "1" else "SMS OFF"
        print(f"  {did}  {state}  {', '.join(routes) or 'NO DELIVERY ROUTE'}")

    print("\nOnly snapshot a config you have confirmed is working.")
    return 0


def cmd_map(cfg: dict) -> int:
    """Show which subaccount each DID is tied to, so sip_config.json can be filled in.

    VoIP.ms field naming for SMS-to-SIP routing varies, so rather than guessing one
    field name this prints every field on the DID whose value looks like a
    subaccount (i.e. contains an underscore, as in 123456_main).
    """
    payload = api(cfg, "getDIDsInfo")
    rows = payload.get("dids", []) or payload.get("data", [])
    if not rows:
        raise VoipmsError("getDIDsInfo returned no DIDs")

    print(f"{len(rows)} DID(s) on this account\n")
    suggestions = []

    for entry in rows:
        did = str(entry.get("did", "")).strip()
        description = str(entry.get("description", "")).strip()
        print(f"  {did}   {description}")

        found = {}
        for key, value in entry.items():
            text = str(value).strip()
            # A subaccount looks like 123456_label; ignore plain numbers and flags.
            if "_" in text and not text.isdigit() and len(text) < 64 and " " not in text:
                print(f"      {key:<28} {text}")
                found[key] = text

        account = (found.get("sms_routing") or found.get("sip_uri")
                   or found.get("routing") or next(iter(found.values()), None))
        if account:
            account = account.split(":")[-1]  # routing values look like "account:123456_x"
            suggestions.append({"label": description or did, "account": account, "did": did})
        else:
            print("      (no subaccount-looking field - check the DID's SMS settings in the portal)")
        print()

    if suggestions:
        print("Skeleton for sip_config.json (add the SIP password for each):\n")
        print(json.dumps({"accounts": suggestions}, indent=2))
    return 0


def load_baseline() -> dict:
    if not os.path.exists(BASELINE_PATH):
        return {}
    with open(BASELINE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    parser = argparse.ArgumentParser(description="VoIP.ms SMS delivery watchdog")
    parser.add_argument("command", choices=["test", "baseline", "check", "repair", "map"])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--dry-run", action="store_true", help="repair: show changes without applying")
    args = parser.parse_args()

    try:
        cfg = load_config()
        if args.command == "test":
            return cmd_test(cfg)
        if args.command == "baseline":
            return cmd_baseline(cfg)
        if args.command == "map":
            return cmd_map(cfg)
        if args.command == "check":
            return report(run_check(cfg, load_baseline()), args.json)
        if args.command == "repair":
            return report(run_repair(cfg, load_baseline(), args.dry_run), args.json)
    except VoipmsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 3
    return 3


if __name__ == "__main__":
    sys.exit(main())
