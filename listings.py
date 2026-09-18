"""A car listing as either site reports it, and the facelift check.

The wanted car is the facelifted Ioniq 5 (sold in Israel from mid-2024):
84 kWh battery, rear wiper. Sellers mis-file cars (a 2024 pre-facelift
listed as 2025, a wrong sub-model picked from the dropdown), so year and
model alone are not trusted: every candidate is judged on the harder
evidence its own listing page carries, and the alert says which evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, replace
from typing import Protocol

# Rated power is the cleanest tell, and both sites print it. The 84 kWh
# facelift is 168 kW / 239 kW; every earlier pack was 160 kW / 225 kW (or
# 125 kW for the 58 kWh). Sites round the horsepower slightly differently.
FACELIFT_HP = {228, 229, 230, 325, 326}
PREFACELIFT_HP = {168, 170, 217, 218, 305, 306}
FACELIFT_MIN_KWH = 80.0

# Israeli trim names. "Elite" was sold on both generations, so it decides
# nothing on its own.
FACELIFT_ONLY_TRIMS = ("limited", "premium", "long range")
PREFACELIFT_ONLY_TRIMS = ("luxury", "prestige", "supreme")

KWH_84 = re.compile(r"\b84\s*(?:kwh|קוט|קילוואט|קוו)", re.I)
FACELIFT_WORD = re.compile(r"פייסליפט|פייס\s*ליפט|facelift|\bFL\b", re.I)
REAR_WIPER = re.compile(r"מגב\s+אחורי|rear\s+wiper", re.I)
HP_IN_TEXT = re.compile(r"(\d{3})\s*(?:כ[\"״׳']?ס|hp|ps)\b", re.I)


@dataclass
class Listing:
    source: str                 # "yad2" | "carwiz"
    id: str
    url: str
    model: str = "ioniq 5"      # carwiz files every Ioniq under one model
    year: int | None = None
    month: int | None = None
    trim: str = ""
    hp: int | None = None
    battery_kwh: float | None = None
    range_km: str = ""
    price: int | None = None
    km: int | None = None
    hand: str = ""
    city: str = ""
    seller: str = ""            # "private" | "dealer" | agency name
    created_at: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        bits = [str(self.year or "?"), self.trim or "?"]
        if self.hp:
            bits.append(f"{self.hp}hp")
        return " ".join(bits)

    def updated_with(self, other: "Listing") -> "Listing":
        """A new listing taking every field `other` has a value for; the
        search-card values stay where the detail page said nothing."""
        newer = {f.name: getattr(other, f.name) for f in fields(self)
                 if getattr(other, f.name) not in (None, "", [])}
        return replace(self, **newer)


@dataclass
class Verdict:
    status: str                 # "confirmed" | "unverified" | "rejected"
    evidence: list[str] = field(default_factory=list)

    @property
    def wanted(self) -> bool:
        return self.status != "rejected"


def hp_from_text(text: str) -> int | None:
    m = HP_IN_TEXT.search(text or "")
    return int(m.group(1)) if m else None


def _wrong_car(lst: Listing) -> str | None:
    """Reasons no amount of other evidence can outweigh: the Ioniq 6 shares
    the facelift's motor rating, and no 2023 car is a facelift."""
    if "5" not in lst.model:
        return f"model is {lst.model}"
    if lst.year is not None and lst.year < 2024:
        return f"year {lst.year} predates the facelift"
    return None


def _hard_evidence(lst: Listing) -> tuple[list[str], list[str]]:
    """(for, against) from data the seller did not type by hand: power and
    battery. Plus the free text, which is weaker but explicit."""
    pro, con = [], []
    if lst.battery_kwh:
        if lst.battery_kwh >= FACELIFT_MIN_KWH:
            pro.append(f"{lst.battery_kwh:g} kWh battery")
        else:
            con.append(f"{lst.battery_kwh:g} kWh battery (pre-facelift pack)")
    if lst.hp in FACELIFT_HP:
        pro.append(f"{lst.hp}hp = 84 kWh facelift motor")
    elif lst.hp in PREFACELIFT_HP:
        con.append(f"{lst.hp}hp = pre-facelift motor")
    text = lst.description or ""
    if KWH_84.search(text):
        pro.append("seller text says 84 kWh")
    if FACELIFT_WORD.search(text):
        pro.append("seller text says facelift")
    return pro, con


def _trim_inference(lst: Listing) -> tuple[str, str]:
    """What the trim name alone says: (status, note)."""
    trim = lst.trim.lower()
    if any(t in trim for t in PREFACELIFT_ONLY_TRIMS):
        return "rejected", f"trim {lst.trim!r} only existed pre-facelift"
    if any(t in trim for t in FACELIFT_ONLY_TRIMS) and (lst.year or 0) >= 2024:
        return "confirmed", f"trim {lst.trim!r} is a facelift-only trim"
    if (lst.year or 0) >= 2025:
        return "unverified", f"{lst.year} model year, but no power/battery shown"
    return "unverified", f"trim {lst.trim or '?'} exists on both generations"


def classify(lst: Listing) -> Verdict:
    wrong = _wrong_car(lst)
    if wrong:
        return Verdict("rejected", [wrong])
    pro, con = _hard_evidence(lst)
    notes = []
    if REAR_WIPER.search(lst.description or ""):
        notes.append("seller text mentions rear wiper")
    if pro and con:
        return Verdict("unverified", pro + con + notes)  # contradicts itself
    if con:
        return Verdict("rejected", con)
    if pro:
        return Verdict("confirmed", pro + notes)
    status, note = _trim_inference(lst)
    return Verdict(status, [note] + notes)


class Source(Protocol):
    """What carwatch drives, one per site: opened for a poll, asked for
    the current search results, then for each unseen candidate's own page."""
    name: str

    def __init__(self, cfg: dict, state_dir) -> None: ...
    def __enter__(self) -> "Source": ...
    def __exit__(self, *exc) -> bool: ...
    def search(self) -> list[Listing]: ...
    def verify(self, lst: Listing) -> Listing | None: ...
