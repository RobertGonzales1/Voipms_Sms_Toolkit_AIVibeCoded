#!/usr/bin/env python3
"""Tests for the watchdog's detection logic. No network, no credentials.

Run:  python test_logic.py
      python -m unittest test_logic -v
      python -m unittest test_logic.RepairCooldownTests -v
"""

import unittest

import voipms_watch
from voipms_watch import (
    CRITICAL,
    INFO,
    MONITORED_FIELDS,
    SETSMS_PARAM_MAP,
    WARNING,
    check_delivery_sanity,
    compare_to_baseline,
    run_repair,
    worst_severity,
)


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


def baseline_from(did_info):
    return {f: did_info.get(f, "") for f in MONITORED_FIELDS}


def severities(alerts):
    return [sev for sev, _ in alerts]


class DeliverySanityTests(unittest.TestCase):

    def test_healthy_did_is_silent(self):
        self.assertEqual(check_delivery_sanity({"5551110000": good_did()}), [])

    def test_sms_disabled_is_critical(self):
        alerts = check_delivery_sanity({"5551110000": good_did(sms_enabled="0")})
        self.assertEqual(severities(alerts), [CRITICAL])

    def test_no_route_warns(self):
        """A DID delivering over SIP legitimately has no email/forward route."""
        alerts = check_delivery_sanity({
            "5551110000": good_did(sms_email_enabled="0", sms_forward_enabled="0")})
        self.assertEqual(severities(alerts), [WARNING])

    def test_forward_enabled_but_number_blank_warns(self):
        alerts = check_delivery_sanity({
            "5551110000": good_did(sms_email_enabled="0", sms_forward="")})
        self.assertEqual(severities(alerts), [WARNING])

    def test_sms_unavailable_warns(self):
        alerts = check_delivery_sanity({"5551110000": good_did(sms_available="0")})
        self.assertEqual(severities(alerts), [WARNING])

    def test_email_only_route_is_fine(self):
        alerts = check_delivery_sanity({
            "5551110000": good_did(sms_forward_enabled="0", sms_forward="")})
        self.assertEqual(alerts, [])


class BaselineComparisonTests(unittest.TestCase):

    def setUp(self):
        self.baseline = {"5551110000": baseline_from(good_did())}

    def test_identical_config_is_silent(self):
        self.assertEqual(compare_to_baseline({"5551110000": good_did()}, self.baseline), [])

    def test_forwarding_turned_off_is_critical(self):
        alerts = compare_to_baseline(
            {"5551110000": good_did(sms_forward_enabled="0")}, self.baseline)
        self.assertIn(CRITICAL, severities(alerts))

    def test_forward_number_blanked_is_critical(self):
        alerts = compare_to_baseline({"5551110000": good_did(sms_forward="")}, self.baseline)
        self.assertIn(CRITICAL, severities(alerts))

    def test_forward_number_changed_is_warning(self):
        alerts = compare_to_baseline(
            {"5551110000": good_did(sms_forward="5559999999")}, self.baseline)
        self.assertEqual(severities(alerts), [WARNING])

    def test_did_removed_from_account_is_critical(self):
        self.assertEqual(severities(compare_to_baseline({}, self.baseline)), [CRITICAL])

    def test_unknown_new_did_is_info_only(self):
        alerts = compare_to_baseline(
            {"5551110000": good_did(), "5552220000": good_did()}, self.baseline)
        self.assertEqual(severities(alerts), [INFO])


class FieldConstantTests(unittest.TestCase):
    """The watch list and the write map are deliberately separate.

    Widening what we monitor must not silently widen what 'repair' overwrites.
    """

    def test_write_map_covers_only_monitored_fields(self):
        self.assertTrue(set(SETSMS_PARAM_MAP).issubset(set(MONITORED_FIELDS)))

    def test_param_names_are_unique(self):
        params = list(SETSMS_PARAM_MAP.values())
        self.assertEqual(len(params), len(set(params)))


class RepairCooldownTests(unittest.TestCase):
    """Regression: the VoIP.ms setSMS cooldown is per DID, not global.

    Sleeping between every DID turned a few seconds of API calls into roughly a
    minute per number - about 19 minutes across a full account.
    """

    def setUp(self):
        self.slept = []
        self.setsms_calls = []
        self.fetch_calls = 0

        drifted = good_did(sms_forward_enabled="0")
        self.repaired = good_did()

        def fake_fetch(cfg):
            # First read shows drift; every read after the repair shows it fixed.
            self.fetch_calls += 1
            state = drifted if self.fetch_calls == 1 else self.repaired
            return {did: dict(state) for did in ("5551110000", "5552220000", "5553330000")}

        def fake_api(cfg, method, **params):
            self.setsms_calls.append((method, params.get("did")))
            return {"status": "success"}

        self._orig = (voipms_watch.fetch_dids, voipms_watch.api, voipms_watch.time.sleep)
        voipms_watch.fetch_dids = fake_fetch
        voipms_watch.api = fake_api
        voipms_watch.time.sleep = self.slept.append

    def tearDown(self):
        voipms_watch.fetch_dids, voipms_watch.api, voipms_watch.time.sleep = self._orig

    def test_distinct_dids_never_sleep(self):
        baseline = {did: baseline_from(good_did())
                    for did in ("5551110000", "5552220000", "5553330000")}
        results = run_repair({}, baseline, dry_run=False)

        self.assertEqual(len(self.setsms_calls), 3)
        self.assertEqual(self.slept, [], "slept between distinct DIDs")
        self.assertTrue(all(sev == INFO for sev, _ in results), results)

    def test_dry_run_makes_no_calls(self):
        baseline = {"5551110000": baseline_from(good_did())}
        run_repair({}, baseline, dry_run=True)
        self.assertEqual(self.setsms_calls, [])
        self.assertEqual(self.slept, [])


class SeverityTests(unittest.TestCase):

    def test_critical_wins(self):
        self.assertEqual(worst_severity([(INFO, "a"), (WARNING, "b"), (CRITICAL, "c")]), CRITICAL)

    def test_warning_beats_info(self):
        self.assertEqual(worst_severity([(INFO, "a"), (WARNING, "b")]), WARNING)

    def test_all_info(self):
        self.assertEqual(worst_severity([(INFO, "a")]), INFO)


if __name__ == "__main__":
    unittest.main(verbosity=2)
