"""
Registry sampler for the Richmond-area small business web presence study.

Draws a REPRODUCIBLE RANDOM SAMPLE of active registered businesses in the
Richmond metro from Virginia's open business-entity data.

Why random and not the lead-finder's own database: the lead finder searches
specific trade categories and is tuned to surface businesses that look
underserved.  That is exactly right for prospecting and exactly wrong for a
published statistic -- it would overstate every "missing X" number.  This
module draws from the full population instead, with a fixed seed so anyone
can reproduce the identical sample from the methodology page.

Source: data.virginia.gov CKAN datastore, State Corporation Commission
business entity files (Corporation + LLC).

Note on the API: the portal's datastore_search_sql endpoint sits behind a WAF
that rejects queries containing trim() and similar function calls with a bare
403.  The filtered datastore_search endpoint is stable, so that is what this
uses.  Values in the source are space-padded, which the filter must match.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field

CKAN_SEARCH = "https://data.virginia.gov/api/3/action/datastore_search"
# Identify the crawler honestly. Set STUDY_USER_AGENT to your own name and URL if you run this.
UA = {"User-Agent": os.environ.get("STUDY_USER_AGENT", "Mozilla/5.0 (compatible; RVAWebPresenceStudy/1.0; +https://github.com/OlafTheDread7/rva-web-presence-study)")}

DATASETS = [
    ("Corporation", "9d79f14f-86cd-4ce2-b2da-2df005958e0d"),
    ("LLC", "6d55efe1-1784-414a-a38e-ce1f6b4be5fe"),
]

# Richmond city + the three surrounding counties, by the place names that
# appear in the registry's City field.
LOCALITY_GROUPS = {
    "Richmond": ["Richmond"],
    "Henrico": ["Henrico", "Glen Allen", "Sandston", "Highland Springs"],
    "Chesterfield": ["Chesterfield", "Midlothian", "North Chesterfield",
                     "South Chesterfield", "Chester", "Moseley", "Bon Air"],
    "Hanover": ["Hanover", "Ashland", "Mechanicsville", "Montpelier"],
}
CITIES = [c for group in LOCALITY_GROUPS.values() for c in group]
CITY_TO_LOCALITY = {c: loc for loc, group in LOCALITY_GROUPS.items() for c in group}

ACTIVE = "ACTIVE    "          # the Status field is space-padded to 10
PAGE = 5000
POLITE_DELAY = 0.6


@dataclass
class Entity:
    entity_id: str
    name: str
    entity_type: str            # Corporation | LLC
    city: str
    locality: str               # Richmond | Henrico | Chesterfield | Hanover
    zip: str
    street: str
    incorp_date: str
    industry_code: str
    ra_name: str = ""
    _sample_rank: int = field(default=0, repr=False)

    def address(self) -> str:
        bits = [self.street.strip(), self.city.strip(), "VA", self.zip.strip()]
        return ", ".join(b for b in bits if b)


def _clean(v) -> str:
    return (v or "").replace("\t", "").strip()


def _get(url: str, tries: int = 4) -> dict:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:            # noqa: BLE001 - portal is flaky
            last = e
            time.sleep(2 + i * 3)
    raise RuntimeError(f"CKAN request failed after {tries} tries: {last!r}")


def _city_filter_values(city: str) -> list[str]:
    """The registry pads short city names; try the widths that occur."""
    seen, out = set(), []
    for w in (10, 0, 12, 18, 20):
        v = city.ljust(w) if w else city
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def fetch_city(res_id: str, city: str, label: str,
               progress=print) -> list[Entity]:
    """All ACTIVE registrations for one city, paginated."""
    chosen, total = None, 0
    for value in _city_filter_values(city):
        filters = json.dumps({"City": value, "Status": ACTIVE})
        url = CKAN_SEARCH + "?" + urllib.parse.urlencode(
            {"resource_id": res_id, "filters": filters, "limit": 0})
        data = _get(url)
        if data.get("success") and data["result"].get("total", 0) > 0:
            chosen, total = value, data["result"]["total"]
            break
        time.sleep(POLITE_DELAY)
    if not chosen:
        progress(f"    {label}/{city}: 0")
        return []

    rows: list[Entity] = []
    offset = 0
    while offset < total:
        filters = json.dumps({"City": chosen, "Status": ACTIVE})
        url = CKAN_SEARCH + "?" + urllib.parse.urlencode({
            "resource_id": res_id, "filters": filters,
            "limit": PAGE, "offset": offset})
        data = _get(url)
        recs = data["result"].get("records", [])
        if not recs:
            break
        for r in recs:
            name = _clean(r.get("Name"))
            if not name:
                continue
            rows.append(Entity(
                entity_id=_clean(r.get("EntityID")),
                name=name,
                entity_type=label,
                city=_clean(r.get("City")),
                locality=CITY_TO_LOCALITY.get(_clean(r.get("City")), "Other"),
                zip=_clean(r.get("Zip")),
                street=" ".join(x for x in (_clean(r.get("Street1")),
                                            _clean(r.get("Street2"))) if x),
                incorp_date=_clean(r.get("IncorpDate")),
                industry_code=_clean(r.get("IndustryCode")),
                ra_name=_clean(r.get("RA-Name")),
            ))
        offset += len(recs)
        time.sleep(POLITE_DELAY)
    progress(f"    {label}/{city}: {len(rows)}")
    return rows


def build_population(progress=print) -> list[Entity]:
    pop: list[Entity] = []
    for label, res_id in DATASETS:
        progress(f"  {label} registry:")
        for city in CITIES:
            pop.extend(fetch_city(res_id, city, label, progress))
    # Same business can appear in both files (converted entity); keep one.
    seen, deduped = set(), []
    for e in pop:
        key = (e.name.upper(), e.zip[:5])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    progress(f"  population: {len(pop)} rows -> {len(deduped)} unique entities")
    return deduped


# Entities that are not "small businesses with customers" in any useful sense.
# Excluded before sampling and reported as an exclusion in the methodology.
EXCLUDE_TOKENS = (
    " HOLDINGS", " HOLDING CO", "PROPERTIES LLC", " REALTY TRUST",
    " FAMILY TRUST", " REVOCABLE", " ESTATE OF", " FOUNDATION",
    " CHURCH", " MINISTRIES", " MINISTRY", " PARISH",
    " HOA", " HOMEOWNERS", " CONDOMINIUM", " CONDO ASSOCIATION",
    " OWNERS ASSOCIATION", " PROPERTY OWNERS",
)


def is_excluded(name: str) -> str:
    u = " " + name.upper()
    for tok in EXCLUDE_TOKENS:
        if tok in u:
            return tok.strip()
    return ""


def draw_sample(population: list[Entity], n: int, seed: int) -> tuple[list[Entity], dict]:
    eligible, excluded = [], {}
    for e in population:
        reason = is_excluded(e.name)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
        else:
            eligible.append(e)
    eligible.sort(key=lambda e: (e.entity_type, e.entity_id, e.name))
    rng = random.Random(seed)
    n = min(n, len(eligible))
    sample = rng.sample(eligible, n)
    for i, e in enumerate(sample, 1):
        e._sample_rank = i
    meta = {
        "population_total": len(population),
        "eligible_total": len(eligible),
        "excluded_total": len(population) - len(eligible),
        "excluded_by_reason": excluded,
        "sample_size": n,
        "seed": seed,
    }
    return sample, meta


def main(n: int = 600, seed: int = 20260825, out: str = "data/sample.json"):
    print("Pulling the Richmond-metro business registry…")
    pop = build_population()
    sample, meta = draw_sample(pop, n, seed)
    by_loc: dict[str, int] = {}
    for e in sample:
        by_loc[e.locality] = by_loc.get(e.locality, 0) + 1
    meta["sample_by_locality"] = by_loc
    meta["population_by_locality"] = {}
    for e in pop:
        meta["population_by_locality"][e.locality] = \
            meta["population_by_locality"].get(e.locality, 0) + 1
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "sample": [asdict(e) for e in sample]}, f, indent=1)
    print(json.dumps(meta, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("-n", type=int, default=600)
    p.add_argument("--seed", type=int, default=20260825)
    p.add_argument("--out", default="data/sample.json")
    a = p.parse_args()
    main(a.n, a.seed, a.out)
