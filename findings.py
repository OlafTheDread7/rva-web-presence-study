"""Print the headline findings in data/stats.json as a Markdown table.

    python findings.py                 # the published 2026 edition
    python findings.py path/to/stats.json

Every figure comes with its 95% Wilson interval and the base it was measured
on, so nobody reads 33.1% as more precise than it is.
"""

from __future__ import annotations

import json
import sys


def table(stats: dict) -> str:
    run, sample = stats["run"], stats["sample"]
    lines = [
        f"Edition {run['report_year']}, run {run['run_date']}. "
        f"Random sample of {sample['sample_size']:,} from {sample['eligible_total']:,} eligible registrations "
        f"(seed {sample['seed']}). {stats['base_n']} confirmed customer-facing businesses, "
        f"{stats['live_sites_n']} with a live website.",
        "",
        "| Finding | Share | 95% interval | Base |",
        "|---|---|---|---|",
    ]
    for m in stats["metrics"].values():
        if not m["base"]:
            continue
        lines.append(f"| {m['label']} | {m['pct']:.1f}% | {m['ci_low']:.1f} to {m['ci_high']:.1f} | {m['base']:,} |")
    lines += ["", "| No website at all, by locality | Share | 95% interval | Base |", "|---|---|---|---|"]
    for name, m in sorted(stats["by_locality"].items(), key=lambda kv: -kv[1]["pct"]):
        lines.append(f"| {name} | {m['pct']:.1f}% | {m['ci_low']:.1f} to {m['ci_high']:.1f} | {m['base']:,} |")
    return "\n".join(lines)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data/stats.json"
    with open(path, encoding="utf-8") as fh:
        print(table(json.load(fh)))
