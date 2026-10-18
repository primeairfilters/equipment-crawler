"""The rules that decide whether what the AI said is really on the page.

The AI can only report what is printed in the document: the model number has to appear in the
text, the number has to appear in the text, the key has to be on the fixed list, and the unit
and size have to make sense. Anything that fails is kept as a failed fact and never reaches the
equipment tables.
"""
import re
from collections import namedtuple
from decimal import Decimal, InvalidOperation

from crawler.store import model_key

# key: (unit choices, smallest, largest). A numeric key has a range; refrigerant is words.
SPEC_KEYS = {
    "seer2": ((), Decimal("6"), Decimal("40")),
    "seer": ((), Decimal("6"), Decimal("40")),
    "eer2": ((), Decimal("5"), Decimal("30")),
    "hspf2": ((), Decimal("4"), Decimal("15")),
    "afue": (("%",), Decimal("50"), Decimal("100")),
    "tonnage": (("tons", "ton"), Decimal("0.5"), Decimal("10")),
    "cooling_capacity": (("btuh", "btu/h", "btu"), Decimal("3000"), Decimal("150000")),
    "heating_capacity": (("btuh", "btu/h", "btu"), Decimal("3000"), Decimal("250000")),
    "airflow": (("cfm",), Decimal("100"), Decimal("6000")),
    "mca": (("a", "amps"), Decimal("1"), Decimal("200")),
    "mocp": (("a", "amps"), Decimal("1"), Decimal("250")),
    "voltage": (("v", "volts"), Decimal("100"), Decimal("700")),
    "weight": (("lbs", "lb"), Decimal("10"), Decimal("1500")),
    "refrigerant_charge": (("oz", "lbs", "lb"), Decimal("1"), Decimal("1000")),
    "sound": (("dba", "db"), Decimal("30"), Decimal("100")),
    "refrigerant": ((), None, None),
}
TEXT_KEYS = {"refrigerant"}
MAX_VALUE_LENGTH = 200
MAX_UNIT_LENGTH = 30

Check = namedtuple("Check", "model_number key value unit ok reason")


def _alnum(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _number_on_page(value, text):
    if not re.fullmatch(r"-?\d+(\.\d+)?", value):
        return False
    candidates = {value}
    try:
        number = Decimal(value)
        if number == number.to_integral_value() and abs(number) >= 1000:
            candidates.add(f"{int(number):,}")
    except InvalidOperation:
        return False
    for candidate in candidates:
        if re.search(r"(?<![\d.])" + re.escape(candidate) + r"(?!\d)(?!\.\d)", text):
            return True
    return False


def _judge(model_number, key, value, unit, page_text, page_alnum, model_ok):
    def result(ok, reason=None):
        return Check(model_number, key, value, unit, ok, reason)

    if not model_ok:
        return result(False, "model_not_on_page")
    if key not in SPEC_KEYS:
        return result(False, "unknown_key")
    units, low, high = SPEC_KEYS[key]
    if value is None or not str(value).strip() or len(str(value)) > MAX_VALUE_LENGTH:
        return result(False, "bad_value")
    value = str(value).strip()
    if unit is not None and (len(str(unit)) > MAX_UNIT_LENGTH or (str(unit).strip() and str(unit).strip().lower() not in units)):
        return result(False, "bad_unit")
    if key in TEXT_KEYS:
        if _alnum(value) not in page_alnum:
            return result(False, "text_not_on_page")
        return result(True)
    if not _number_on_page(value, page_text):
        return result(False, "number_not_on_page")
    if not (low <= Decimal(value) <= high):
        return result(False, "out_of_range")
    return result(True)


def verify(output, texts):
    """One Check per fact in the answer, in the answer's order."""
    page_text = "\n".join(texts)
    page_alnum = _alnum(page_text)
    checks = []
    for entry in (output or {}).get("models", []):
        number = str(entry.get("model_number") or "")
        key = model_key(number)
        model_ok = bool(key) and key in page_alnum
        for spec in entry.get("specs", []):
            value = spec.get("value")
            checks.append(_judge(number, str(spec.get("key") or ""), None if value is None else str(value),
                                 spec.get("unit"), page_text, page_alnum, model_ok))
    return checks
