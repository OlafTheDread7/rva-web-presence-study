"""
Sector classification for the RVA Metro Small Business Web Report.

Google Places assigns each business a primaryTypeDisplayName, and across a
random registry sample that fragments hard -- 105 distinct types across 574
businesses, with only three clearing 30 and one of those ("Services") being a
catch-all that means nothing.  Reporting per Places type is therefore not
possible from a registry-drawn sample; reporting per *sector* is.

These groupings are deliberately coarse and published on the methodology page
so anyone can disagree with them specifically.  A type that matches nothing is
left unclassified and counted as such rather than being forced into a bucket --
"Services", "Point of interest" and "Building" carry no sector information and
inventing one for them would be making the data say something it doesn't.
"""

from __future__ import annotations

# Order matters: the first family whose keyword matches wins, so the more
# specific families are listed before the broader ones.
FAMILIES: dict[str, list[str]] = {
    "Home & Construction": [
        "general contractor", "roofing", "plumb", "electric", "hvac", "heating",
        "air condition", "landscap", "lawn", "paint", "floor", "remodel",
        "construction", "carpent", "masonry", "fence", "pool", "pest",
        "handyman", "window", "door", "cabinet", "concrete", "siding",
        "gutter", "tile", "excavat", "demolition", "insulation", "solar",
    ],
    "Health & Medical": [
        "health", "medical", "doctor", "dentist", "dental", "clinic",
        "chiroprac", "therap", "veterinar", "pharmac", "optome", "optic",
        "nursing", "counsel", "psycholog", "orthodont", "podiat", "audiolog",
    ],
    "Personal Care & Beauty": [
        "hair salon", "beauty salon", "nail", "barber", "spa", "massage",
        "tattoo", "fitness", "gym", "yoga", "wellness center", "skin care",
    ],
    "Food & Hospitality": [
        "restaurant", "cafe", "coffee", "bakery", "brewery", "winery",
        "caterer", "catering", "food", "hotel", "motel", "pizza", "deli",
        "ice cream", "bar and grill",
    ],
    "Auto & Transport": [
        "car repair", "auto", "mover", "transport", "towing", "truck", "tire",
        "car dealer", "car wash", "logistics", "courier",
    ],
    "Real Estate & Property": [
        "real estate", "apartment", "property man", "realt", "leasing",
        "storage",
    ],
    "Professional Services": [
        "consultant", "attorney", "lawyer", "law firm", "account", "insurance",
        "financ", "banking", "bank", "engineer", "architect", "marketing",
        "advertis", "staffing", "employment", "tax", "notary", "photograph",
        "design", "software", "information technology",
    ],
    "Retail": [
        "store", "shop", "boutique", "retail", "wholesal", "market", "dealer",
        "florist", "jewel", "furniture",
    ],
    "Community & Nonprofit": [
        "non-profit", "nonprofit", "church", "association", "organization",
        "school", "educational", "charit", "museum", "library", "religious",
        "community center",
    ],
}

# Types that exist in the data but carry no sector meaning.  Named explicitly
# so they read as a deliberate exclusion rather than a gap in the keyword list.
UNINFORMATIVE = {"services", "point of interest", "building", "establishment",
                 "", "none"}

MIN_REPORTABLE = 30


def classify(places_type: str | None) -> str | None:
    """Sector for a Places type, or None if it carries no sector information."""
    t = (places_type or "").strip().lower()
    if t in UNINFORMATIVE:
        return None
    for name, keys in FAMILIES.items():
        if any(k in t for k in keys):
            return name
    return None


def summarize(confirmed: list[dict], stat) -> dict:
    """Per-sector metrics. `stat` is stats.stat, injected to avoid a cycle."""
    buckets: dict[str, list[dict]] = {}
    unclassified = 0
    for r in confirmed:
        sec = classify(r.get("places_type"))
        if sec is None:
            unclassified += 1
        else:
            buckets.setdefault(sec, []).append(r)

    out, thin = {}, {}
    for sec, rows in buckets.items():
        n = len(rows)
        live = [r for r in rows
                if r.get("has_website") and r.get("site_reachable")
                and not r.get("site_social_only")]
        entry = {
            "n": n,
            "live_n": len(live),
            "no_website": stat(f"No website — {sec}",
                               sum(1 for r in rows if not r.get("has_website")), n),
            "no_schema": stat(f"No Schema.org — {sec}",
                              sum(1 for r in live if not r.get("site_schema_present")),
                              len(live)),
            "no_llms": stat(f"No llms.txt — {sec}",
                            sum(1 for r in live if not r.get("site_llms_txt")),
                            len(live)),
            "few_reviews": stat(f"Under 10 reviews — {sec}",
                                sum(1 for r in rows
                                    if isinstance(r.get("review_count"), int)
                                    and r["review_count"] < 10), n),
        }
        (out if n >= MIN_REPORTABLE else thin)[sec] = entry

    ranked = dict(sorted(out.items(),
                         key=lambda kv: -kv[1]["no_website"]["pct"]))
    return {
        "reportable": ranked,
        "below_threshold": {k: v["n"] for k, v in
                            sorted(thin.items(), key=lambda kv: -kv[1]["n"])},
        "unclassified": unclassified,
        "min_reportable": MIN_REPORTABLE,
        "classified_total": sum(len(rows) for rows in buckets.values()),
    }
