import base64
import gzip
import json
import unittest

from scoring import evaluate, parse_report_lines, score_market, select_keep


def make_report(market, ips, target="ws_api"):
    return {
        "market": market,
        "targets": [{
            "name": target,
            "ips": [{"ip": ip, "rtt_us": {"count": count, "p50": p50, "p99": p99}}
                    for ip, p50, p99, count in ips],
        }],
    }


def encode_line(market, report):
    payload = base64.b64encode(gzip.compress(json.dumps(report).encode())).decode()
    return f"REPORT {market} {payload}"


class ScoreMarketTest(unittest.TestCase):
    def test_picks_lowest_p50_ip_and_tracks_worst(self):
        report = make_report("futures", [("1.1.1.1", 900.0, 1500.0, 200),
                                         ("2.2.2.2", 600.0, 2000.0, 200),
                                         ("3.3.3.3", 700.0, 900.0, 200)])
        score = score_market(report, "futures")
        self.assertTrue(score.ok)
        self.assertEqual(score.best_ip, "2.2.2.2")
        self.assertEqual(score.p50_us, 600.0)
        self.assertEqual(score.p99_us, 2000.0)
        self.assertEqual(score.worst_ip_p50_us, 900.0)

    def test_ignores_ips_below_min_samples(self):
        report = make_report("spot", [("1.1.1.1", 100.0, 200.0, 10)])
        score = score_market(report, "spot", min_samples=50)
        self.assertFalse(score.ok)
        self.assertIn("samples", score.reason)

    def test_missing_target(self):
        report = make_report("spot", [("1.1.1.1", 100.0, 200.0, 100)], target="rest")
        self.assertFalse(score_market(report, "spot").ok)


class EvaluateTest(unittest.TestCase):
    def reports(self, futures_p50, spot_p50):
        return {
            "futures": make_report("futures", [("1.1.1.1", futures_p50, futures_p50 * 2, 200)]),
            "spot": make_report("spot", [("2.2.2.2", spot_p50, spot_p50 * 2, 200)]),
        }

    def test_passes_without_thresholds(self):
        score = evaluate("i-1", self.reports(500, 600), ["futures", "spot"],
                         {"futures": None, "spot": None})
        self.assertTrue(score.passed)
        self.assertEqual(score.rank_key(), (1100.0, 2200.0))

    def test_threshold_failure_is_reported(self):
        score = evaluate("i-1", self.reports(500, 900), ["futures", "spot"],
                         {"futures": 800.0, "spot": 800.0})
        self.assertFalse(score.passed)
        self.assertIn("spot: p50", score.reason)

    def test_missing_market_report_fails(self):
        reports = self.reports(500, 600)
        del reports["spot"]
        score = evaluate("i-1", reports, ["futures", "spot"], {"futures": None, "spot": None})
        self.assertFalse(score.passed)
        self.assertIn("spot: no report", score.reason)


class SelectKeepTest(unittest.TestCase):
    def test_orders_passing_by_rank(self):
        thresholds = {"futures": None}
        fast = evaluate("i-fast", {"futures": make_report("futures", [("a", 300, 500, 100)])},
                        ["futures"], thresholds)
        slow = evaluate("i-slow", {"futures": make_report("futures", [("a", 700, 900, 100)])},
                        ["futures"], thresholds)
        broken = evaluate("i-broken", {}, ["futures"], thresholds)
        self.assertEqual([s.instance_id for s in select_keep([slow, broken, fast], 2)],
                         ["i-fast", "i-slow"])
        self.assertEqual(select_keep([slow, fast], 0), [])


class ParseReportLinesTest(unittest.TestCase):
    def test_round_trip_and_corrupt_line(self):
        report = make_report("futures", [("1.1.1.1", 1.0, 2.0, 100)])
        stdout = "\n".join(["CHRONY Stratum : 3", encode_line("futures", report),
                            "REPORT spot not-base64!!", "NOREPORT spot"])
        reports, errors = parse_report_lines(stdout)
        self.assertEqual(reports, {"futures": report})
        self.assertEqual(len(errors), 1)
        self.assertIn("spot", errors[0])


if __name__ == "__main__":
    unittest.main()
