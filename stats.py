"""Aggregate audited records into publishable statistics with error bars."""

from __future__ import annotations

import math

import sectors


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Wilson score interval — behaves at small n and near 0%/100%,
    which the normal approximation does not."""
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round(p * 100, 1), round(max(0.0, centre - half) * 100, 1), \
        round(min(1.0, centre + half) * 100, 1)


def stat(label: str, k: int, n: int, note: str = "") -> dict:
    pct, lo, hi = wilson(k, n)
    return {"label": label, "count": k, "base": n, "pct": pct,
            "ci_low": lo, "ci_high": hi,
            "margin": round((hi - lo) / 2, 1), "note": note}


def build(audited: list[dict], sample_meta: dict, run_meta: dict) -> dict:
    n_all = len(audited)
    keyed = run_meta.get("places_enabled", False)

    # Layer 1 — the registry population.
    identified = [r for r in audited if r.get("gbp") is True]
    no_presence = [r for r in audited if r.get("gbp") is False]

    # Layer 2 — customer-facing businesses.  A Google Business Profile is the
    # screen: it is the only signal in the data that separates a business with
    # customers from a holding entity or a single-property LLC.  Without a
    # Places key there is no screen, so layer 2 falls back to the whole sample
    # and every figure is marked provisional.
    base = identified if keyed else audited
    nb = len(base)

    with_site = [r for r in base if r.get("has_website")]
    live = [r for r in with_site if r.get("site_reachable")
            and not r.get("site_social_only")]

    metrics = {
        "no_website": stat(
            "No website at all", nb - len(with_site), nb,
            "Neither Google Business Profile nor a domain probe found a site."
            if keyed else
            "Free-source probe only — understates website presence."),
        "social_only": stat(
            "Social page instead of a website",
            sum(1 for r in with_site if r.get("site_social_only")), nb),
        "no_https": stat(
            "No valid HTTPS",
            sum(1 for r in live if not r.get("site_https")), len(live)),
        "not_mobile": stat(
            "No mobile viewport",
            sum(1 for r in live if not r.get("site_mobile_viewport")), len(live)),
        "no_schema": stat(
            "No Schema.org structured data",
            sum(1 for r in live if not r.get("site_schema_present")), len(live)),
        "no_localbusiness": stat(
            "No LocalBusiness/Organization schema",
            sum(1 for r in live if not r.get("site_has_localbusiness")), len(live)),
        "no_llms_txt": stat(
            "No llms.txt",
            sum(1 for r in live if not r.get("site_llms_txt")), len(live)),
        "blocks_ai": stat(
            "Blocks at least one AI crawler",
            sum(1 for r in live if r.get("site_ai_blocked")), len(live)),
        "no_robots": stat(
            "No robots.txt at all",
            sum(1 for r in live if not r.get("site_robots_found")), len(live)),
    }

    if keyed:
        # Denominator is the businesses Places actually answered for, not the
        # whole sample -- a handful of lookups fail with a 5xx each run and
        # counting them as "no profile" would quietly pad the figure.
        metrics["no_gbp"] = stat(
            "No Google Business Profile found", len(no_presence),
            len(identified) + len(no_presence),
            "Registered entities with no matching Maps listing. Includes "
            "businesses trading under a name unlike their registered name.")
        metrics["no_reviews"] = stat(
            "Fewer than 10 Google reviews",
            sum(1 for r in identified
                if isinstance(r.get("review_count"), int)
                and r["review_count"] < 10), len(identified))

    slow = [r for r in live if isinstance(r.get("site_ttfb_ms"), int)]
    slow.sort(key=lambda r: r["site_ttfb_ms"])
    median_ttfb = slow[len(slow) // 2]["site_ttfb_ms"] if slow else None

    platforms: dict[str, int] = {}
    for r in live:
        p = r.get("site_platform") or "Custom / unidentified"
        platforms[p] = platforms.get(p, 0) + 1

    by_locality: dict[str, dict] = {}
    for loc in ("Richmond", "Henrico", "Chesterfield", "Hanover"):
        rows = [r for r in base if r.get("locality") == loc]
        if len(rows) >= 30:
            k = sum(1 for r in rows if not r.get("has_website"))
            by_locality[loc] = stat(f"No website — {loc}", k, len(rows))

    schema_types: dict[str, int] = {}
    for r in live:
        for t in (r.get("site_schema_types") or []):
            schema_types[t] = schema_types.get(t, 0) + 1

    sector_view = sectors.summarize(identified, stat) if keyed else None

    return {
        "run": run_meta,
        "sample": sample_meta,
        "sectors": sector_view,
        "layer1": {
            "population_total": sample_meta.get("population_total"),
            "eligible_total": sample_meta.get("eligible_total"),
            "sample_size": n_all,
            "identified_customer_facing": len(identified),
            "no_google_presence": len(no_presence),
        },
        "base_n": nb,
        "live_sites_n": len(live),
        "metrics": metrics,
        "median_ttfb_ms": median_ttfb,
        "platforms": dict(sorted(platforms.items(), key=lambda kv: -kv[1])),
        "schema_types": dict(sorted(schema_types.items(),
                                    key=lambda kv: -kv[1])[:12]),
        "by_locality": by_locality,
    }
