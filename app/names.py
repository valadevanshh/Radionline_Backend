"""t86: doctor / referrer name hygiene. "Dr" is a title, never part of the typed name.

clean_dr("DR. DR. JAYESH") -> "DR. JAYESH" (repeated prefixes collapse to one; a single one is kept)
strip_dr("Dr.Dr. Jayesh") -> "Jayesh"
Applied on save (so "Dr. Dr. X" can't be stored) and on read (cleans existing rows, no migration).
"""
import re
from typing import Optional

_DR = re.compile(r"^\s*(?:(?:dr|doctor)(?:\s*\.\s*|\s+))+(?=\S)", re.IGNORECASE)


def strip_dr(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    s = str(name).strip()
    return _DR.sub("", s).strip() or s


def clean_dr(name: Optional[str]) -> Optional[str]:
    if not name:
        return name
    s = str(name).strip()
    m = _DR.match(s)
    if not m:
        return s
    title = "DR." if s[:2] == "DR" else "Dr."
    return f"{title} {s[m.end():].strip()}"
