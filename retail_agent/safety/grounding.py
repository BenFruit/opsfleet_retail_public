"""Numeric grounding: figures in model prose must come from checked query results.

LLMs are unreliable at mental arithmetic (e.g. adding twenty category rows into
a quarter total). The app already appends the authoritative tables; this check
catches prose figures that match neither a checked value nor the sum/difference
of two checked values. Percentages are not checked (they are usually derived).
This is a deterministic heuristic, not a proof of correctness.
"""

import re
from bisect import bisect_left
from decimal import Decimal, InvalidOperation

# Money-like or count-like figures: 1,234 / 1,234.56 / 12.5 / $85 / 172.
# Not part of a date, identifier, version or percentage.
_FIGURE = re.compile(r"(?<![\w.\-/:])\$?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+)(?![\w\-/:%]|\.\d|\s*%|\s*percent)")


def _parse(text: str) -> float | None:
    try:
        return float(Decimal(text.replace(",", "")))
    except (InvalidOperation, ValueError):
        return None


def figures(text: str) -> list[tuple[str, float, float]]:
    """(raw, value, absolute tolerance) for figures worth checking."""
    found = []
    for match in _FIGURE.finditer(text):
        raw, number = match.group(0), match.group(1)
        value = _parse(number)
        if value is None:
            continue
        decimals = len(number.split(".")[1]) if "." in number else 0
        is_year = decimals == 0 and "," not in number and 1900 <= value <= 2100
        # Small integers are ranks, list sizes or day counts ("top 5", "30 days").
        if is_year or (decimals == 0 and value < 100 and not raw.startswith("$")):
            continue
        # A rounded figure may differ from the exact value by half its last digit.
        tolerance = max(0.5 * 10**-decimals, 0.005 * abs(value))
        found.append((raw, value, tolerance))
    return found


def checked_values(observations: list[dict]) -> list[float]:
    values = []
    for item in observations:
        for row in item.get("rows", []):
            for cell in row.values():
                if isinstance(cell, bool) or cell is None:
                    continue
                number = cell if isinstance(cell, (int, float)) else _parse(str(cell))
                if number is not None:
                    values.append(float(number))
    return sorted(values)


def _near(sorted_values: list[float], target: float, tolerance: float) -> bool:
    index = bisect_left(sorted_values, target - tolerance)
    return index < len(sorted_values) and sorted_values[index] <= target + tolerance


def ungrounded_figures(prose: str, observations: list[dict], *, context: str = "") -> list[str]:
    """Figures in prose that are not a checked value, nor a sum/difference of two.

    ``context`` (e.g. the user's question) supplies numbers the user stated.
    Returns [] when there are no checked values: nothing to ground against.
    """
    values = checked_values(observations)
    if not values:
        return []
    stated = {value for _, value, _ in figures(context)}
    missing = []
    for raw, value, tolerance in figures(prose):
        if value in stated or _near(values, value, tolerance):
            continue
        # value = a + b  or  value = |a - b|  for two checked values.
        if any(_near(values, value - a, tolerance) or _near(values, a - value, tolerance) for a in values):
            continue
        missing.append(raw)
    return list(dict.fromkeys(missing))
