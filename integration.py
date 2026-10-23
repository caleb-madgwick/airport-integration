"""Normalise two fictional ground handlers' feeds into one canonical operational model.

Flow: source adapter -> canonical record -> validation -> accepted | needs_review | rejected.
"""
import csv
import io
import json
import re
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# --- Shared: canonical model, reference data and validation --------------------------

STATUSES = {"SCHEDULED", "DELAYED", "CANCELLED", "DEPARTED"}
AIRPORTS = {"SYD": ("YSSY", "Australia/Sydney"),  # IATA -> (ICAO, IANA timezone)
            "MEL": ("YMML", "Australia/Melbourne"),
            "PER": ("YPPH", "Australia/Perth")}
ICAO_TO_IATA = {icao: iata for iata, (icao, _) in AIRPORTS.items()}


@dataclass(frozen=True)
class Flight:
    flight_key: str  # carrier + number + local operating date, e.g. "XA123-2026-10-23"
    carrier: str
    flight_number: str
    origin: str  # IATA codes
    destination: str
    scheduled_departure_utc: datetime
    estimated_departure_utc: Optional[datetime]
    status: str
    source_updated_utc: datetime
    customer: str = ""  # provenance is stamped by the pipeline, not by adapters
    source_ref: str = ""


@dataclass(frozen=True)
class BaggageTransfer:
    bag_tag: str  # 10-digit IATA licence plate
    inbound_flight_key: str
    outbound_flight_key: str
    source_updated_utc: datetime
    customer: str = ""
    source_ref: str = ""


def validate(record):
    """Customer-independent rules every canonical record must pass. Returns error strings."""
    errors = []
    for name, value in vars(record).items():
        if value in ("", None) and name != "estimated_departure_utc":
            errors.append(f"{name} is required")
        elif isinstance(value, datetime) and value.utcoffset() != timedelta(0):
            errors.append(f"{name} is not in UTC")
    if isinstance(record, Flight):
        for name in ("origin", "destination"):
            if getattr(record, name) not in AIRPORTS:
                errors.append(f"{name} {getattr(record, name)!r} is not a known airport")
        if record.origin == record.destination:
            errors.append("origin and destination are the same")
        if record.status not in STATUSES:
            errors.append(f"status {record.status!r} is not one of {sorted(STATUSES)}")
    elif not re.fullmatch(r"[0-9]{10}", record.bag_tag):
        errors.append(f"bag_tag {record.bag_tag!r} is not a 10-digit licence plate")
    return errors


# --- Shared: normalisation helpers used by adapters ------------------------------------

def lookup(table, key, what):
    if key not in table:
        raise ValueError(f"unknown {what} {key!r}")
    return table[key]


def parse_flight_id(text):
    """'XA0123', 'XA 123' or 'xa123' -> ('XA', '123')."""
    match = re.fullmatch(r"([A-Z0-9]{2})\s*0*([1-9][0-9]{0,3})", text.strip().upper())
    if not match:
        raise ValueError(f"unrecognised flight number {text!r}")
    return match.group(1), match.group(2)


def flight_key(carrier, number, operating_date):
    """A flight number repeats daily, so its identity includes the operating date."""
    return f"{carrier}{number}-{operating_date.isoformat()}"


def operating_date(scheduled_utc, origin):
    """Airline convention: the operating date is the local scheduled departure date."""
    return scheduled_utc.astimezone(ZoneInfo(lookup(AIRPORTS, origin, "airport")[1])).date()


def parse_utc(text):
    """ISO 8601 timestamp that must carry an offset -> UTC. Never assumes a timezone."""
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))  # 3.9 rejects a bare "Z"
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {text!r} has no UTC offset")
    return parsed.astimezone(timezone.utc)


def local_to_utc(naive, tz_name):
    """Wall-clock time -> UTC, rejecting times a daylight-saving change repeats or skips."""
    local = naive.replace(tzinfo=ZoneInfo(tz_name))
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        raise ValueError(f"{naive} is ambiguous or does not exist in {tz_name}")
    return local.astimezone(timezone.utc)


# --- Customer-specific: the only code that knows a customer's format -------------------

KESTREL_STATUS = {"SCH": "SCHEDULED", "DLY": "DELAYED", "CNX": "CANCELLED", "DEP": "DEPARTED"}
BANKSIA_STATUS = {"on time": "SCHEDULED", "delayed": "DELAYED",
                  "cancelled": "CANCELLED", "departed": "DEPARTED"}


def kestrel_flight(raw, config):
    """Airline API: nested JSON, UTC 'Z' timestamps, three-letter status codes."""
    carrier, number = parse_flight_id(raw["flt"]["carrier"] + raw["flt"]["number"])
    origin, scheduled = raw["route"]["from"], parse_utc(raw["sched_dep"])
    return Flight(
        flight_key(carrier, number, operating_date(scheduled, origin)), carrier, number,
        origin, raw["route"]["to"], scheduled,
        estimated_departure_utc=parse_utc(raw["est_dep"]) if raw.get("est_dep") else None,
        status=KESTREL_STATUS.get(raw["status_code"], raw["status_code"]),  # unmapped -> invalid
        source_updated_utc=parse_utc(raw["updated"]))


def kestrel_bag(row, config):
    """Baggage CSV: local time with no offset, dd/mm/yyyy dates, combined flight IDs."""
    def key(flight_column, date_column):
        carrier, number = parse_flight_id(row[flight_column])
        return flight_key(carrier, number, datetime.strptime(row[date_column], "%d/%m/%Y").date())

    last_scan = datetime.strptime(row["last_scan"], "%d/%m/%Y %H:%M")
    return BaggageTransfer(row["bag_tag"].strip(), key("inbound_flight", "inbound_date"),
                           key("outbound_flight", "outbound_date"),
                           local_to_utc(last_scan, config["timezone"]))


def banksia_flight(raw, config):
    """Flight API: flat JSON, ICAO airports, offset timestamps, free-text status, epoch updates."""
    carrier, number = parse_flight_id(raw["flightId"])
    origin = lookup(ICAO_TO_IATA, raw["dep"], "ICAO airport")
    scheduled, estimated = parse_utc(raw["scheduledDeparture"]), raw.get("estimatedDeparture")
    return Flight(
        flight_key(carrier, number, operating_date(scheduled, origin)), carrier, number,
        origin, lookup(ICAO_TO_IATA, raw["arr"], "ICAO airport"), scheduled,
        estimated_departure_utc=parse_utc(estimated) if estimated else None,
        status=BANKSIA_STATUS.get(raw["status"].strip().lower(), raw["status"]),
        source_updated_utc=datetime.fromtimestamp(raw["lastModified"], timezone.utc))


# Onboarding a customer adds its adapters above and one entry to each table below.
CUSTOMERS = {"kestrel": {"timezone": "Australia/Sydney"},  # Kestrel Ground Handling, SYD
             "banksia": {"timezone": "Australia/Perth"}}  # Banksia Airside Services, PER
ADAPTERS = {  # (customer, source) -> (split payload into raw records, adapter)
    ("kestrel", "flight_api"): (lambda payload: payload["flights"], kestrel_flight),
    ("kestrel", "baggage_csv"): (lambda payload: csv.DictReader(io.StringIO(payload)), kestrel_bag),
    ("banksia", "flight_api"): (lambda payload: payload, banksia_flight),
}


# --- Pipeline ------------------------------------------------------------------------

def review_reason(record, flights):
    """Cross-source check: a transfer bag must be heading for a known, operating flight."""
    if isinstance(record, BaggageTransfer):
        outbound = flights.get((record.customer, record.outbound_flight_key))
        if outbound is None:
            return f"outbound flight {record.outbound_flight_key} is not in the flight feed"
        if outbound.status == "CANCELLED":
            return f"outbound flight {record.outbound_flight_key} is cancelled"
    return None


def run(data):
    """Process every feed. Each record ends in exactly one of the three outcomes."""
    result, valid = {"accepted": [], "needs_review": [], "rejected": []}, []
    for feed in data["feeds"]:
        customer, source = feed["customer"], feed["source"]
        split, adapt = ADAPTERS[(customer, source)]  # an unregistered feed is a setup error
        for n, raw in enumerate(split(feed["payload"]), start=1):
            ref = f"{customer}/{source}#{n}"
            try:
                record = replace(adapt(raw, CUSTOMERS[customer]), customer=customer, source_ref=ref)
                errors = validate(record)
            except KeyError as exc:
                errors = [f"missing field {exc.args[0]!r}"]
            except (TypeError, ValueError, AttributeError) as exc:
                errors = [f"malformed value: {exc}"]
            if errors:
                result["rejected"].append({"source_ref": ref, "errors": errors, "raw": raw})
            else:
                valid.append(record)
    flights = {(r.customer, r.flight_key): r for r in valid if isinstance(r, Flight)}
    for record in valid:
        reason = review_reason(record, flights)
        if reason:
            result["needs_review"].append({"record": record, "reason": reason})
        else:
            result["accepted"].append(record)
    return result


def to_json(value):
    if is_dataclass(value):
        return {"type": type(value).__name__, **asdict(value)}
    return value.isoformat()  # datetimes


if __name__ == "__main__":
    result = run(json.loads(Path(__file__).with_name("sample_data.json").read_text()))
    print(json.dumps(result, indent=2, default=to_json))
    print(" | ".join(f"{outcome}: {len(items)}" for outcome, items in result.items()))
