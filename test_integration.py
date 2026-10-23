import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from integration import (
    CUSTOMERS, Flight, banksia_flight, kestrel_bag, kestrel_flight, local_to_utc, run, validate,
)

KESTREL_FLIGHT = {
    "id": "KFA-1", "flt": {"carrier": "XA", "number": "0123"}, "route": {"from": "SYD", "to": "PER"},
    "sched_dep": "2026-10-23T03:05:00Z", "est_dep": "2026-10-23T03:40:00Z",
    "status_code": "DLY", "updated": "2026-10-23T02:10:00Z",
}
BANKSIA_FLIGHT = {  # the same flight, as Banksia's API describes it
    "flightId": "xa 123", "dep": "YSSY", "arr": "YPPH",
    "scheduledDeparture": "2026-10-23T14:05:00+11:00",
    "estimatedDeparture": "2026-10-23T14:40:00+11:00",
    "status": "Delayed", "lastModified": 1792721400,
}
BAG_HEADER = "bag_tag,inbound_flight,inbound_date,outbound_flight,outbound_date,last_scan\n"


def feeds(*feeds):
    return {"feeds": [{"customer": c, "source": s, "payload": p} for c, s, p in feeds]}


class AdapterTests(unittest.TestCase):
    def test_kestrel_flight_converts_to_canonical_record(self):
        flight = kestrel_flight(KESTREL_FLIGHT, CUSTOMERS["kestrel"])
        self.assertEqual(flight.flight_key, "XA123-2026-10-23")
        self.assertEqual((flight.origin, flight.destination, flight.status), ("SYD", "PER", "DELAYED"))
        self.assertEqual(flight.scheduled_departure_utc, datetime(2026, 10, 23, 3, 5, tzinfo=timezone.utc))
        accepted = run(feeds(("kestrel", "flight_api", {"flights": [KESTREL_FLIGHT]})))["accepted"]
        self.assertEqual(accepted[0].source_ref, "kestrel/flight_api#1")

    def test_same_flight_in_two_customer_formats_gives_identical_records(self):
        self.assertEqual(kestrel_flight(KESTREL_FLIGHT, CUSTOMERS["kestrel"]),
                         banksia_flight(BANKSIA_FLIGHT, CUSTOMERS["banksia"]))

    def test_flight_key_uses_local_operating_date_not_utc_date(self):
        # 07:30 in Sydney on 23 Oct is 20:30 UTC on 22 Oct.
        raw = dict(KESTREL_FLIGHT, sched_dep="2026-10-22T20:30:00Z")
        self.assertEqual(kestrel_flight(raw, CUSTOMERS["kestrel"]).flight_key, "XA123-2026-10-23")


class TimezoneTests(unittest.TestCase):
    def test_sydney_local_time_converts_to_utc_during_daylight_saving(self):
        row = {"bag_tag": "0081234567", "inbound_flight": "XC0456", "inbound_date": "23/10/2026",
               "outbound_flight": "XA0123", "outbound_date": "23/10/2026",
               "last_scan": "23/10/2026 12:48"}
        bag = kestrel_bag(row, CUSTOMERS["kestrel"])
        self.assertEqual(bag.source_updated_utc, datetime(2026, 10, 23, 1, 48, tzinfo=timezone.utc))

    def test_skipped_and_repeated_local_times_are_rejected(self):
        for label, naive in [("clocks go forward", datetime(2026, 10, 4, 2, 30)),
                             ("clocks go back", datetime(2026, 4, 5, 2, 30))]:
            with self.subTest(label), self.assertRaises(ValueError):
                local_to_utc(naive, "Australia/Sydney")

    def test_timestamp_without_offset_is_rejected_not_assumed(self):
        raw = dict(BANKSIA_FLIGHT, scheduledDeparture="2026-10-23T14:05:00")
        result = run(feeds(("banksia", "flight_api", [raw])))
        self.assertEqual(result["accepted"], [])
        self.assertIn("has no UTC offset", result["rejected"][0]["errors"][0])


class ValidationTests(unittest.TestCase):
    def test_missing_field_is_rejected_with_source_reference(self):
        raw = {k: v for k, v in KESTREL_FLIGHT.items() if k != "sched_dep"}
        rejected = run(feeds(("kestrel", "flight_api", {"flights": [raw]})))["rejected"]
        self.assertEqual(rejected[0]["source_ref"], "kestrel/flight_api#1")
        self.assertEqual(rejected[0]["errors"], ["missing field 'sched_dep'"])

    def test_unmapped_status_code_is_rejected_not_guessed(self):
        raw = dict(KESTREL_FLIGHT, status_code="HLD")
        result = run(feeds(("kestrel", "flight_api", {"flights": [raw]})))
        self.assertEqual(result["accepted"], [])
        self.assertIn("status 'HLD'", result["rejected"][0]["errors"][0])

    def test_validation_catches_adapter_that_forgets_utc_conversion(self):
        local = datetime.fromisoformat("2026-10-23T14:05:00+11:00")
        flight = Flight("XA123-2026-10-23", "XA", "123", "SYD", "PER", local, None,
                        "SCHEDULED", local, "kestrel", "test#1")
        self.assertEqual(validate(flight), ["scheduled_departure_utc is not in UTC",
                                            "source_updated_utc is not in UTC"])

    def test_bag_onto_cancelled_flight_needs_review(self):
        bags = BAG_HEADER + "0081234569,XC0456,23/10/2026,XA0123,23/10/2026,23/10/2026 13:02\n"
        cancelled = dict(KESTREL_FLIGHT, status_code="CNX")
        result = run(feeds(("kestrel", "flight_api", {"flights": [cancelled]}),
                           ("kestrel", "baggage_csv", bags)))
        self.assertEqual([r.status for r in result["accepted"]], ["CANCELLED"])
        self.assertEqual(result["needs_review"][0]["reason"], "outbound flight XA123-2026-10-23 is cancelled")


class DuplicateTests(unittest.TestCase):
    def test_identical_duplicate_is_accepted_once(self):
        resent = dict(KESTREL_FLIGHT, updated="2026-10-23T02:15:00Z")  # same content, re-sent
        result = run(feeds(("kestrel", "flight_api", {"flights": [KESTREL_FLIGHT, resent]})))
        self.assertEqual([r.source_ref for r in result["accepted"]], ["kestrel/flight_api#1"])
        self.assertEqual(result["rejected"][0]["source_ref"], "kestrel/flight_api#2")
        self.assertEqual(result["rejected"][0]["errors"], ["identical duplicate of kestrel/flight_api#1"])

    def test_conflicting_records_are_held_for_review_in_any_order(self):
        cancelled = dict(KESTREL_FLIGHT, status_code="CNX")
        bags = BAG_HEADER + "0081234567,XC0456,23/10/2026,XA0123,23/10/2026,23/10/2026 12:48\n"
        for order in ([KESTREL_FLIGHT, cancelled], [cancelled, KESTREL_FLIGHT]):
            with self.subTest(first_status=order[0]["status_code"]):
                result = run(feeds(("kestrel", "flight_api", {"flights": order}),
                                   ("kestrel", "baggage_csv", bags)))
                self.assertEqual(result["accepted"], [])
                reasons = [item["reason"] for item in result["needs_review"]]
                self.assertEqual(reasons[:2], ["conflicting records for XA123-2026-10-23: "
                                               "kestrel/flight_api#1, kestrel/flight_api#2"] * 2)
                self.assertEqual(reasons[2], "outbound flight XA123-2026-10-23 has conflicting records")


class SampleDataTests(unittest.TestCase):
    def test_every_sample_record_has_exactly_one_outcome(self):
        data = json.loads((Path(__file__).parent / "sample_data.json").read_text())
        result = run(data)
        self.assertEqual({k: len(v) for k, v in result.items()},
                         {"accepted": 7, "needs_review": 2, "rejected": 6})
        refs = ([r.source_ref for r in result["accepted"]]
                + [i["record"].source_ref for i in result["needs_review"]]
                + [i["source_ref"] for i in result["rejected"]])
        self.assertEqual(len(refs), len(set(refs)))
        self.assertTrue(all(validate(r) == [] for r in result["accepted"]))


if __name__ == "__main__":
    unittest.main()
