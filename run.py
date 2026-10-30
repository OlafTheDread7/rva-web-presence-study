"""Orchestrator: sample -> audit -> stats -> CSV.

    python run.py --sample          # draw a fresh random sample
    python run.py --audit           # audit the sample (needs GOOGLE_API_KEY)
    python run.py --all             # both
    python build_site.py            # render the report

Set GOOGLE_API_KEY before --audit.  Without it the run still completes, but
every figure is marked provisional: Places is what separates real customer-
facing businesses from holding entities, and it is the only source for the
Google Business Profile metric.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time

import audit
import sampler
import stats

DATA = "data"
CSV_FIELDS = [
    "_sample_rank", "entity_id", "name", "entity_type", "locality", "city",
    "zip", "incorp_date", "gbp", "places_name", "rating", "review_count",
    "places_type", "has_website", "website", "website_source",
    "site_reachable", "site_https", "site_platform", "site_mobile_viewport",
    "site_ttfb_ms", "site_page_kb", "site_schema_present",
    "site_has_localbusiness", "site_schema_types", "site_llms_txt",
    "site_robots_found", "site_ai_blocked", "site_social_only", "site_error",
    "probe_confidence", "probe_evidence",
]


def do_sample(n: int, seed: int):
    os.makedirs(DATA, exist_ok=True)
    sampler.main(n, seed, f"{DATA}/sample.json")


def do_audit(use_psi: bool, workers: int):
    with open(f"{DATA}/sample.json", encoding="utf-8") as f:
        blob = json.load(f)
    sample, sample_meta = blob["sample"], blob["meta"]
    keyed = bool(audit.GOOGLE_API_KEY)
    print(f"Auditing {len(sample)} businesses "
          f"({'Google Places enabled' if keyed else 'FREE SOURCES ONLY — provisional'})…")
    t0 = time.time()
    audited = audit.run(sample, use_psi=use_psi, workers=workers)
    run_meta = {
        "places_enabled": keyed,
        "pagespeed_enabled": bool(use_psi and keyed),
        "elapsed_s": round(time.time() - t0, 1),
        "audited": len(audited),
        "run_date": time.strftime("%Y-%m-%d"),
        "report_year": time.strftime("%Y"),
    }
    with open(f"{DATA}/audited.json", "w", encoding="utf-8") as f:
        json.dump({"run": run_meta, "records": audited}, f, indent=1)

    s = stats.build(audited, sample_meta, run_meta)
    with open(f"{DATA}/stats.json", "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)

    # Public CSV: the registered business name and its public web presence.
    # No phone numbers, no contact people, no scoring — this is a dataset,
    # not a lead list, and publishing it as one keeps it that way.
    with open(f"{DATA}/rva-web-presence.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in audited:
            row = dict(r)
            if isinstance(row.get("site_schema_types"), list):
                row["site_schema_types"] = "|".join(row["site_schema_types"])
            if isinstance(row.get("site_ai_blocked"), list):
                row["site_ai_blocked"] = "|".join(row["site_ai_blocked"])
            w.writerow(row)

    print(f"\n  {run_meta['elapsed_s']}s  base n={s['base_n']}  "
          f"live sites={s['live_sites_n']}")
    for key, m in s["metrics"].items():
        if m["base"]:
            print(f"    {m['label']:42s} {m['pct']:5.1f}%  "
                  f"(±{m['margin']:.1f}, n={m['base']})")
    print(f"\nwrote {DATA}/audited.json, {DATA}/stats.json, "
          f"{DATA}/rva-web-presence.csv")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--sample", action="store_true")
    p.add_argument("--audit", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("-n", type=int, default=600)
    p.add_argument("--seed", type=int, default=20260825)
    p.add_argument("--psi", action="store_true",
                   help="also run PageSpeed Insights (slow, needs key)")
    p.add_argument("--workers", type=int, default=audit.WORKERS)
    a = p.parse_args()
    if a.all or a.sample:
        do_sample(a.n, a.seed)
    if a.all or a.audit:
        do_audit(a.psi, a.workers)
    if not (a.all or a.sample or a.audit):
        p.print_help()
