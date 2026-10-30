"""Tests for the parts of the study that don't need the network.

    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import audit  # noqa: E402
import findings  # noqa: E402
import sampler  # noqa: E402
import sectors  # noqa: E402
import stats  # noqa: E402


class Wilson(unittest.TestCase):
    def test_reproduces_the_published_headline(self):
        # 190 of 574 customer-facing businesses had no website: 33.1% (29.4 to 37.1)
        self.assertEqual(stats.wilson(190, 574), (33.1, 29.4, 37.1))
        # Henrico: 64 of 157
        self.assertEqual(stats.wilson(64, 157), (40.8, 33.4, 48.6))

    def test_edges(self):
        self.assertEqual(stats.wilson(0, 0), (0.0, 0.0, 0.0))
        pct, lo, hi = stats.wilson(0, 40)
        self.assertEqual((pct, lo), (0.0, 0.0))
        self.assertGreater(hi, 0)  # an empty count still has uncertainty above zero
        pct, lo, hi = stats.wilson(40, 40)
        self.assertEqual((pct, hi), (100.0, 100.0))
        self.assertLess(lo, 100)


def entity(i, name, etype="LLC", locality="Richmond"):
    return sampler.Entity(entity_id=f"E{i:05d}", name=name, entity_type=etype, city=locality,
                          locality=locality, zip="23220", street="", incorp_date="2020-01-01",
                          industry_code="")


class Sampling(unittest.TestCase):
    def setUp(self):
        self.pop = [entity(i, f"Business {i} LLC") for i in range(200)]
        self.pop += [entity(900, "Smith Family Holdings LLC"), entity(901, "Grace Church Inc"),
                     entity(902, "Oak Ridge Homeowners Association")]

    def test_exclusions_are_counted_not_sampled(self):
        sample, meta = sampler.draw_sample(self.pop, 50, seed=1)
        self.assertEqual(meta["excluded_total"], 3)
        self.assertEqual(meta["excluded_by_reason"], {"HOLDINGS": 1, "CHURCH": 1, "HOMEOWNERS": 1})
        self.assertFalse(any(sampler.is_excluded(e.name) for e in sample))

    def test_same_seed_same_sample_whatever_the_input_order(self):
        a, _ = sampler.draw_sample(list(self.pop), 50, seed=20260825)
        self.assertEqual([e._sample_rank for e in a], list(range(1, 51)))
        a = [e.entity_id for e in a]
        b = [e.entity_id for e in sampler.draw_sample(list(reversed(self.pop)), 50, seed=20260825)[0]]
        c = [e.entity_id for e in sampler.draw_sample(list(self.pop), 50, seed=7)[0]]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class Build(unittest.TestCase):
    @staticmethod
    def record(has_site, gbp=True, locality="Richmond", **site):
        r = {"gbp": gbp, "locality": locality, "has_website": has_site, "places_type": "plumber",
             "review_count": 4}
        if has_site:
            r.update({"site_reachable": True, "site_social_only": False, "site_https": True,
                      "site_mobile_viewport": True, "site_schema_present": False,
                      "site_has_localbusiness": False, "site_llms_txt": False,
                      "site_robots_found": True, "site_ai_blocked": [], "site_ttfb_ms": 300})
            r.update(site)
        return r

    def test_customer_facing_base_and_live_sites(self):
        recs = [self.record(False) for _ in range(30)] + [self.record(True) for _ in range(60)]
        recs += [self.record(True, site_https=False) for _ in range(10)]
        recs += [self.record(False, gbp=False) for _ in range(25)]  # no Maps listing: not in the base
        out = stats.build(recs, {"population_total": 1000, "eligible_total": 900},
                          {"places_enabled": True})
        self.assertEqual(out["base_n"], 100)
        self.assertEqual(out["live_sites_n"], 70)
        self.assertEqual(out["metrics"]["no_website"]["count"], 30)
        self.assertEqual(out["metrics"]["no_https"]["count"], 10)
        self.assertEqual(out["metrics"]["no_gbp"]["base"], 125)
        self.assertEqual(out["by_locality"]["Richmond"]["pct"], 30.0)

    def test_without_places_everything_is_provisional(self):
        recs = [self.record(False, gbp=None) for _ in range(5)]
        out = stats.build(recs, {}, {"places_enabled": False})
        self.assertEqual(out["base_n"], 5)
        self.assertNotIn("no_gbp", out["metrics"])
        self.assertIn("Free-source probe only", out["metrics"]["no_website"]["note"])


class Helpers(unittest.TestCase):
    def test_domain_candidates(self):
        self.assertEqual(audit.norm_name("Acme Plumbing & Heating, LLC"), "acme plumbing heating")
        cands = audit.domain_candidates("Acme Plumbing & Heating, LLC")
        self.assertIn("acmeplumbingheating.com", cands)
        self.assertIn("acme-plumbing-heating.com", cands)
        self.assertLessEqual(len(cands), audit.MAX_CANDIDATES)

    def test_schema_types_from_json_ld(self):
        html = ('<script type="application/ld+json">{"@type":"Plumber","name":"x"}</script>'
                '<script type="application/ld+json">[{"@type":["Organization","LocalBusiness"]}]</script>')
        self.assertEqual(audit._schema_types(html), ["Plumber", "Organization", "LocalBusiness"])

    def test_sectors(self):
        self.assertEqual(sectors.classify("plumber"), "Home & Construction")
        self.assertIsNone(sectors.classify("point of interest"))
        self.assertIsNone(sectors.classify(None))


class PublishedEdition(unittest.TestCase):
    def test_findings_table_renders_from_the_published_stats(self):
        with open(os.path.join(ROOT, "data", "stats.json"), encoding="utf-8") as fh:
            text = findings.table(json.load(fh))
        self.assertIn("| No website at all | 33.1% | 29.4 to 37.1 | 574 |", text)
        self.assertIn("| Henrico | 40.8% | 33.4 to 48.6 | 157 |", text)


if __name__ == "__main__":
    unittest.main()
