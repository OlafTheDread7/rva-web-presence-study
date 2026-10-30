# rva-web-presence-study: notes for the reviewer

Public repo. The pipeline behind a published study of Richmond-area small business websites. Python scripts run by hand; nothing here deploys.

- `sampler.py` pulls public business registry data from data.virginia.gov. `audit.py` looks each business up in Google Places (key in `GOOGLE_API_KEY`, never committed) and probes candidate domains with plain HTTP GETs. `stats.py` and `sectors.py` aggregate.
- Run output with business names and registered agents (`data/sample.json`, `data/audited.json`, the CSV) is gitignored. Only aggregate `data/stats.json` and a chart are committed.

What matters most: the key leaking into output or logs, and the domain probe being steered somewhere it shouldn't go. It only builds domains from business names and fetches them as a crawler.
