import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from .errors import Invalid, check


KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
STAGES = ("mold", "grind", "coat", "assemble", "inspect")


def key(value, name="key"):
    check(isinstance(value, str) and bool(KEY.fullmatch(value)), f"{name} must be a simple identifier", field=name)
    return value


def label(value, name="label", limit=200):
    check(isinstance(value, str) and 0 < len(value.strip()) <= limit,
          f"{name} requires 1-{limit} characters", field=name)
    return value.strip()


def iso_day(value, name="day"):
    check(isinstance(value, str), f"{name} must be a date", field=name)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise Invalid(f"{name} must be an ISO date") from exc
    check(parsed.isoformat() == value, f"{name} must use YYYY-MM-DD", field=name)
    return value


def utc(value, name="timestamp"):
    check(isinstance(value, str), f"{name} must be an ISO timestamp", field=name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Invalid(f"{name} must be an ISO timestamp") from exc
    check(parsed.tzinfo is not None and parsed.utcoffset() is not None,
          f"{name} needs a timezone", field=name)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def number(value, name="number", nonnegative=False, positive=False):
    check(not isinstance(value, bool), f"{name} must be numeric", field=name)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise Invalid(f"{name} must be numeric") from exc
    check(result.is_finite(), f"{name} must be finite", field=name)
    if nonnegative:
        check(result >= 0, f"{name} cannot be negative", field=name)
    if positive:
        check(result > 0, f"{name} must be positive", field=name)
    check(len(result.as_tuple().digits) <= 15 and abs(result.as_tuple().exponent) <= 6,
          f"{name} exceeds supported precision", field=name)
    return result


def decstr(value):
    return format(value, "f")


def obj(value, name="object"):
    check(isinstance(value, dict), f"{name} must be a JSON object", field=name)
    return value


def seq(value, name="items", limit=1000):
    check(isinstance(value, list) and 0 < len(value) <= limit,
          f"{name} must have 1-{limit} items", field=name)
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def stage(value):
    check(value in STAGES, "unsupported production stage", stage=value)
    return value


def json_row(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise Invalid("stored JSON is corrupt") from exc

