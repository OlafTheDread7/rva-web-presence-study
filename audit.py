"""
Four-metric audit for the Richmond-area small business web presence study.

For each sampled business:
  1. WEBSITE      does a working website exist at all?
  2. MOBILE/SPEED viewport meta, HTTPS, time-to-first-byte, page weight
  3. AI-READINESS Schema.org JSON-LD, llms.txt, AI-crawler access in robots.txt
  4. GBP          is there a Google Business Profile?

Two independent sources decide question 1, because neither alone is honest:

  Google Places  -- authoritative when the business has a profile, and the
                    only way to answer question 4.  Requires GOOGLE_API_KEY.
  Domain probe   -- catches businesses with a site but no Places record.
                    Ported from my lead-finding tool, which learned the
                    hard way that an empty website field is not proof of
                    absence.  Only accepts a domain that proves
                    it belongs to THIS business.

A business counts as "has a website" if either source finds one.  A business
counts as "no website" only after BOTH looked and neither found one -- that
distinction is what makes the published number defensible.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")

# Identify the crawler honestly. Set STUDY_USER_AGENT to your own name and URL if you run this.
UA = {"User-Agent": os.environ.get("STUDY_USER_AGENT", "Mozilla/5.0 (compatible; RVAWebPresenceStudy/1.0; +https://github.com/OlafTheDread7/rva-web-presence-study)")}
TIMEOUT = 12
PROBE_TIMEOUT = 8
WORKERS = 12

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/"

# Billing note.  Google charges Text Search at the highest tier of ANY field
# requested.  websiteUri, rating and userRatingCount are Enterprise fields
# ($35/1k, 1,000 free calls a month); name/address/type are Pro ($32/1k, 5,000
# free).  Asking for everything in one call therefore bills EVERY search at
# Enterprise, including the ~76% of registry entities that turn out to have no
# Maps listing at all.
#
# So this runs in two stages.  Stage one is a Pro-tier search that only answers
# "does this business exist on Maps?" -- cheap, and usually free.  Stage two
# pulls the Enterprise fields by place ID, only for the businesses that passed
# stage one.  Same data, roughly a quarter of the cost.
FIND_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress",
    "places.primaryTypeDisplayName",
])
DETAILS_MASK = "websiteUri,rating,userRatingCount,businessStatus"
PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

SOCIAL_DOMAINS = ("facebook.com", "instagram.com", "linktr.ee", "business.site",
                  "yelp.com", "m.facebook.com", "linkedin.com", "nextdoor.com")

AI_CRAWLERS = ["GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended",
               "CCBot", "anthropic-ai", "OAI-SearchBot"]

# --------------------------------------------------------------------------
# Name / domain helpers (ported from my lead-finding tool)
# --------------------------------------------------------------------------

_SUFFIXES = re.compile(
    r"\b(incorporated|corporation|limited liability company|"
    r"professional limited liability company|"
    r"inc|llc|l\.l\.c|l\.c|corp|company|co|ltd|limited|pllc|p\.c|pc|lp|llp)\b\.?",
    re.I)

_GENERIC_TOKENS = {
    "of", "and", "for", "at", "in", "on", "to", "by", "a", "an",
    "va", "virginia", "richmond", "henrico", "chesterfield", "hanover",
    "services", "service", "group", "holdings", "enterprises", "solutions",
    "ventures", "partners", "associates", "management", "properties",
}

_PARKED_MARKERS = (
    "domain is for sale", "buy this domain", "this domain may be for sale",
    "godaddy.com/forsale", "hugedomains", "afternic", "sedoparking",
    "parked free, courtesy", "domain parking", "future home of something",
    "porkbun.com/domain", "namecheap.com/domain", "checkout the full domain",
    "welcome to nginx", "apache2 ubuntu default page", "index of /",
)

_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")

RESOLVE_TLDS = (".com", ".org", ".net", ".co", ".biz", ".us")
MAX_CANDIDATES = 12


def norm_name(s: str) -> str:
    s = (s or "").lower()
    s = _SUFFIXES.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s


def visible_text(html: str) -> str:
    t = _SCRIPT_STYLE.sub(" ", html or "")
    t = _TAG.sub(" ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", t).lower()


def domain_candidates(name: str) -> list[str]:
    raw = [t for t in re.split(r"[^a-z0-9]+", norm_name(name)) if t]
    sig = [t for t in raw if t not in _GENERIC_TOKENS] or raw
    if not sig:
        return []
    bases: list[str] = []

    def add(parts, sep=""):
        b = sep.join(parts)
        if len(b) >= 4 and b not in bases:
            bases.append(b)

    add(sig)
    if len(sig) > 1:
        add(sig, "-")
    if len(sig) > 2:
        add(sig[:2])
        add(sig[:-1])
    if raw != sig:
        add(raw)
        # A locality can be the brand ("Richmond Montessori"), so keep a
        # form that retains the generic words in leading position.
        if len(raw) > 1:
            add(raw[:2])
        if len(raw) > 2:
            add(raw[:3])
    # TLD-major ordering: try every name form on .com before spending
    # requests on .biz for the first form only.  Name form is the stronger
    # signal, so it must not be starved by TLD expansion.
    out = []
    for tld in RESOLVE_TLDS:
        for b in bases:
            out.append(b + tld)
            if len(out) >= MAX_CANDIDATES:
                return out
    return out


def _fetch(url: str, timeout=TIMEOUT):
    try:
        return requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
    except Exception:
        return None


def _fetch_candidate(domain: str):
    for scheme in ("https://", "http://"):
        r = _fetch(scheme + domain, PROBE_TIMEOUT)
        if r is not None and r.status_code < 400 and len(r.text or "") > 500:
            return r
    return None


def _identity_score(resp, name: str, address: str, phone: str,
                    full_name_domain: bool) -> tuple[int, list[str]]:
    raw = (resp.text or "")[:400_000].lower()
    text = visible_text(raw)
    if any(p in text or p in raw[:4000] for p in _PARKED_MARKERS):
        return 0, ["parked/placeholder page"]

    pts, why = 0, []
    addr = (address or "").lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", norm_name(name))
              if len(t) > 2 and t not in _GENERIC_TOKENS]
    full_name = False
    if tokens:
        hits = sum(1 for t in tokens if t in text)
        if hits == len(tokens):
            full_name = True
            pts += 2; why.append("full name on page")
        elif hits >= max(1, len(tokens) - 1):
            pts += 1; why.append(f"name {hits}/{len(tokens)}")
    if full_name_domain:
        pts += 2; why.append("domain is the registered name")

    hard = False
    m = re.match(r"\s*(\d+)\s+([a-z0-9]+)", addr)
    if m and re.search(rf"\b{re.escape(m.group(1))}\b", text) and m.group(2) in text:
        hard = True; pts += 3; why.append("street address matches")

    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        forms = (digits, f"{digits[:3]}-{digits[3:6]}-{digits[6:]}",
                 f"({digits[:3]}) {digits[3:6]}-{digits[6:]}",
                 f"{digits[:3]}.{digits[3:6]}.{digits[6:]}")
        if any(f in text for f in forms):
            hard = True; pts += 3; why.append("phone matches")

    zips = re.findall(r"\b(\d{5})\b", addr)
    geo_strong = bool(
        re.search(r"\bvirginia\b", text)
        or re.search(r"[a-z]\s*,\s*va\b", text)
        or any(re.search(rf"\b{z}\b", text) for z in zips))
    if geo_strong:
        pts += 1; why.append("Virginia address on page")

    if not hard and not geo_strong and len(tokens) < 2:
        return 0, ["single-word name, no corroborating signal"]

    confirmed = hard or (full_name_domain and full_name and geo_strong
                         and len(tokens) >= 2)
    return pts, why + (["confirmed"] if confirmed else [])


def probe_website(name: str, address: str, phone: str) -> dict:
    out = {"probe_url": "", "probe_confidence": "", "probe_evidence": "",
           "probe_n": 0}
    cands = domain_candidates(name)
    out["probe_n"] = len(cands)
    if not cands:
        return out
    exact = cands[0].rsplit(".", 1)[0]
    best = None
    for dom in cands:
        resp = _fetch_candidate(dom)
        if resp is None:
            continue
        stem = dom.rsplit(".", 1)[0]
        pts, why = _identity_score(resp, name, address, phone,
                                   full_name_domain=(stem == exact))
        if pts <= 0:
            continue
        if best is None or pts > best[0]:
            best = (pts, resp.url, why)
        if "confirmed" in why and pts >= 6:
            break
    if best is None:
        return out
    pts, url, why = best
    out["probe_url"] = url
    out["probe_confidence"] = "confirmed" if "confirmed" in why else "possible"
    out["probe_evidence"] = "; ".join(w for w in why if w != "confirmed")
    return out


# --------------------------------------------------------------------------
# Google Places  (question 4, and the authoritative answer to question 1)
# --------------------------------------------------------------------------

def _places_find(name: str, address: str) -> tuple[dict | None, str]:
    """Stage one (Pro tier): is this business on Google Maps at all?"""
    try:
        r = requests.post(PLACES_URL, headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_API_KEY,
            "X-Goog-FieldMask": FIND_MASK,
        }, json={"textQuery": f"{name} {address}".strip(), "pageSize": 5},
            timeout=TIMEOUT)
    except Exception as e:
        return None, type(e).__name__
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"

    target = set(t for t in norm_name(name).split() if len(t) > 2)
    for p in r.json().get("places", []):
        got = set(t for t in norm_name(
            p.get("displayName", {}).get("text", "")).split() if len(t) > 2)
        if not target or not got:
            continue
        if len(target & got) / max(1, len(target)) >= 0.6:
            return p, ""
    return None, ""


def _place_details(place_id: str) -> tuple[dict, str]:
    """Stage two (Enterprise tier): the fields that actually cost money."""
    try:
        r = requests.get(PLACE_DETAILS_URL + place_id, headers={
            "X-Goog-Api-Key": GOOGLE_API_KEY,
            "X-Goog-FieldMask": DETAILS_MASK,
        }, timeout=TIMEOUT)
    except Exception as e:
        return {}, type(e).__name__
    if r.status_code != 200:
        return {}, f"details HTTP {r.status_code}"
    return r.json(), ""


def places_lookup(name: str, address: str) -> dict:
    """Best-match Places record for a registered business, or empty."""
    out = {"gbp": None, "places_url": "", "places_name": "", "rating": "",
           "review_count": "", "places_type": "", "places_error": ""}
    if not GOOGLE_API_KEY:
        out["places_error"] = "no api key"
        return out

    match, err = _places_find(name, address)
    if err:
        out["places_error"] = err
        return out
    if match is None:
        out["gbp"] = False
        return out

    out["gbp"] = True
    out["places_name"] = match.get("displayName", {}).get("text", "")
    out["places_type"] = match.get("primaryTypeDisplayName", {}).get("text", "")

    det, derr = _place_details(match.get("id", ""))
    if derr:
        out["places_error"] = derr
        return out
    out["places_url"] = det.get("websiteUri", "")
    out["rating"] = det.get("rating", "")
    out["review_count"] = det.get("userRatingCount", 0)
    return out


# --------------------------------------------------------------------------
# Site audit  (questions 2 and 3)
# --------------------------------------------------------------------------

_JSONLD = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                     re.I | re.S)


def _schema_types(html: str) -> list[str]:
    types: list[str] = []
    for block in _JSONLD.findall(html or "")[:20]:
        try:
            data = json.loads(block.strip())
        except Exception:
            m = re.findall(r'"@type"\s*:\s*"([^"]+)"', block)
            types.extend(m)
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                t = node.get("@type")
                if isinstance(t, str):
                    types.append(t)
                elif isinstance(t, list):
                    types.extend(x for x in t if isinstance(x, str))
                stack.extend(v for v in node.values()
                             if isinstance(v, (dict, list)))
    seen, out = set(), []
    for t in types:
        if t not in seen:
            seen.add(t); out.append(t)
    return out[:12]


def _robots_ai_policy(origin: str) -> dict:
    out = {"robots_found": False, "ai_blocked": [], "ai_allowed_all": None}
    r = _fetch(origin + "/robots.txt", 8)
    if r is None or r.status_code >= 400 or "text" not in r.headers.get("content-type", ""):
        return out
    out["robots_found"] = True
    body = (r.text or "")[:100_000]
    blocked = []
    # crude but adequate: does a group for this agent disallow everything?
    groups = re.split(r"(?im)^\s*user-agent\s*:", body)
    for g in groups[1:]:
        agent = g.splitlines()[0].strip().lower()
        rules = g.lower()
        blanket = re.search(r"^\s*disallow\s*:\s*/\s*$", rules, re.M)
        for bot in AI_CRAWLERS:
            if agent == bot.lower() and blanket:
                blocked.append(bot)
        if agent == "*" and blanket:
            blocked.append("* (all crawlers)")
    out["ai_blocked"] = sorted(set(blocked))
    out["ai_allowed_all"] = not out["ai_blocked"]
    return out


def audit_site(url: str) -> dict:
    out = {
        "reachable": False, "final_url": "", "https": False, "status": "",
        "social_only": False, "platform": "", "mobile_viewport": False,
        "ttfb_ms": "", "page_kb": "", "title": "",
        "schema_present": False, "schema_types": [], "has_localbusiness": False,
        "llms_txt": False, "robots_found": False, "ai_blocked": [],
        "error": "",
    }
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(d in host for d in SOCIAL_DOMAINS):
        out["social_only"] = True
        out["final_url"] = url
        out["reachable"] = True
        return out
    t0 = time.time()
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
    except requests.exceptions.SSLError:
        out["error"] = "SSL error"
        return out
    except Exception as e:
        out["error"] = type(e).__name__
        return out

    out["reachable"] = r.status_code < 400
    out["status"] = r.status_code
    out["final_url"] = r.url
    out["https"] = r.url.lower().startswith("https")
    out["ttfb_ms"] = int(r.elapsed.total_seconds() * 1000)
    body = r.text or ""
    out["page_kb"] = round(len(r.content) / 1024, 1)
    low = body[:600_000].lower()

    if 'name="viewport"' in low or "name='viewport'" in low:
        out["mobile_viewport"] = True
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if m:
        out["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:160]

    headers = " ".join(f"{k}:{v}" for k, v in r.headers.items()).lower()
    if "x-wix-" in headers or "wixstatic.com" in low:
        out["platform"] = "Wix"
    elif "squarespace" in low:
        out["platform"] = "Squarespace"
    elif "weebly" in low:
        out["platform"] = "Weebly"
    elif "wp-content" in low or "wp-includes" in low:
        out["platform"] = "WordPress"
    elif ("data-ux=" in low or "img1.wsimg.com/website" in low
          or "godaddysites.com" in low):
        out["platform"] = "GoDaddy Builder"
    elif "shopify" in low:
        out["platform"] = "Shopify"

    types = _schema_types(body[:600_000])
    out["schema_types"] = types
    out["schema_present"] = bool(types)
    out["has_localbusiness"] = any(
        t in ("LocalBusiness", "Organization", "ProfessionalService", "Store",
              "Restaurant", "HomeAndConstructionBusiness", "Dentist",
              "MedicalBusiness", "LegalService", "Plumber", "Electrician")
        for t in types)

    parsed = urllib.parse.urlparse(r.url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    lr = _fetch(origin + "/llms.txt", 8)
    out["llms_txt"] = bool(
        lr is not None and lr.status_code == 200
        and "text/html" not in lr.headers.get("content-type", "")
        and len(lr.text or "") > 20)
    out.update({k: v for k, v in _robots_ai_policy(origin).items()
                if k in ("robots_found", "ai_blocked")})
    return out


def pagespeed(url: str) -> int | None:
    if not GOOGLE_API_KEY:
        return None
    try:
        r = requests.get(PSI_URL, params={
            "url": url, "strategy": "mobile", "category": "performance",
            "key": GOOGLE_API_KEY}, timeout=90)
        if r.status_code != 200:
            return None
        return round(r.json()["lighthouseResult"]["categories"]["performance"]["score"] * 100)
    except Exception:
        return None


# --------------------------------------------------------------------------
# One business, end to end
# --------------------------------------------------------------------------

def audit_business(ent: dict, use_psi: bool = False) -> dict:
    name = ent.get("name", "")
    street = ent.get("street", "")
    address = ", ".join(x for x in [street, ent.get("city", "").strip(), "VA",
                                    ent.get("zip", "")] if x)
    rec = dict(ent)
    rec["address"] = address

    pl = places_lookup(name, address)
    rec.update(pl)
    phone = ""

    website = (pl.get("places_url") or "").strip()
    source = "google places" if website else ""

    if not website:
        pr = probe_website(name, address, phone)
        rec.update(pr)
        if pr["probe_url"] and pr["probe_confidence"] == "confirmed":
            website = pr["probe_url"]
            source = "domain probe (confirmed)"
        elif pr["probe_url"]:
            rec["probe_possible_only"] = True
    else:
        rec.update({"probe_url": "", "probe_confidence": "not needed",
                    "probe_evidence": "", "probe_n": 0})

    rec["website"] = website
    rec["website_source"] = source
    rec["has_website"] = bool(website)

    if website:
        rec.update({f"site_{k}": v for k, v in audit_site(website).items()})
        if not rec.get("site_reachable"):
            rec["has_website"] = bool(rec.get("site_social_only"))
        if use_psi and rec.get("site_reachable") and not rec.get("site_social_only"):
            rec["psi_mobile"] = pagespeed(website)
    return rec


def run(sample: list[dict], progress=print, use_psi: bool = False,
        workers: int = WORKERS) -> list[dict]:
    out, done = [], 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(audit_business, e, use_psi): e for e in sample}
        for fut in as_completed(futs):
            done += 1
            try:
                out.append(fut.result())
            except Exception as e:
                ent = futs[fut]
                out.append({**ent, "audit_error": f"{type(e).__name__}: {e}"})
            if done % 25 == 0 or done == len(sample):
                progress(f"  audited {done}/{len(sample)}")
    out.sort(key=lambda r: r.get("_sample_rank", 0))
    return out
