#!/usr/bin/env python3
"""Offline tests for the watchdog's detection logic. No network, no credentials.

Run:  python test_logic.py
"""

import sys

from voipms_watch import (
    CRITICAL,
    INFO,
    WARNING,
    check_delivery_sanity,
    compare_to_baseline,
    worst_severity,
)

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def good_did(**overrides):
    base = {
        "sms_enabled": "1",
        "sms_email_enabled": "1",
        "sms_email": "me@example.com",
        "sms_forward_enabled": "1",
        "sms_forward": "5551230000",
        "sms_url_callback_enabled": "0",
        "sms_url_callback": "",
        "sms_available": "1",
        "description": "test",
    }
    base.update(overrides)
    return base


def severities(alerts):
    return [sev for sev, _ in alerts]


print("check_delivery_sanity")

alerts = check_delivery_sanity({"5551110000": good_did()})
check("healthy DID produces no alerts", alerts == [], alerts)

alerts = check_delivery_sanity({"5551110000": good_did(sms_enabled="0")})
check("SMS disabled -> CRITICAL", severities(alerts) == [CRITICAL], alerts)

alerts = check_delivery_sanity({
    "5551110000": good_did(sms_email_enabled="0", sms_forward_enabled="0")
})
check("no delivery route -> WARNING", severities(alerts) == [WARNING], alerts)

# The silent killer: toggle still on, but the destination was blanked out.
alerts = check_delivery_sanity({
    "5551110000": good_did(sms_email_enabled="0", sms_forward="")
})
check("forward enabled but number blank -> WARNING", severities(alerts) == [WARNING], alerts)

alerts = check_delivery_sanity({"5551110000": good_did(sms_available="0")})
check("SMS not available on DID -> WARNING", severities(alerts) == [WARNING], alerts)

# Only one route is needed, not all three.
alerts = check_delivery_sanity({
    "5551110000": good_did(sms_forward_enabled="0", sms_forward="")
})
check("email-only route is fine", alerts == [], alerts)


print("\ncompare_to_baseline")

baseline = {"5551110000": {k: v for k, v in good_did().items() if k.startswith("sms_")
                           and k not in ("sms_available",)}}

alerts = compare_to_baseline({"5551110000": good_did()}, baseline)
check("identical config -> no alerts", alerts == [], alerts)

alerts = compare_to_baseline({"5551110000": good_did(sms_forward_enabled="0")}, baseline)
check("forwarding turned off -> CRITICAL", CRITICAL in severities(alerts), alerts)

alerts = compare_to_baseline({"5551110000": good_did(sms_forward="")}, baseline)
check("forward number blanked -> CRITICAL", CRITICAL in severities(alerts), alerts)

alerts = compare_to_baseline({"5551110000": good_did(sms_forward="5559999999")}, baseline)
check("forward number changed -> WARNING", severities(alerts) == [WARNING], alerts)

alerts = compare_to_baseline({}, baseline)
check("DID vanished from account -> CRITICAL", severities(alerts) == [CRITICAL], alerts)

alerts = compare_to_baseline({"5551110000": good_did(), "5552220000": good_did()}, baseline)
check("new unknown DID -> INFO only", severities(alerts) == [INFO], alerts)


print("\nworst_severity")
check("critical wins", worst_severity([(INFO, "a"), (WARNING, "b"), (CRITICAL, "c")]) == CRITICAL)
check("warning over info", worst_severity([(INFO, "a"), (WARNING, "b")]) == WARNING)
check("all info", worst_severity([(INFO, "a")]) == INFO)


print()
if FAILURES:
    print(f"{len(FAILURES)} test(s) FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("All tests passed.")
