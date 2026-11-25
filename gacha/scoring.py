"""Pure scoring helpers for probe reports (no AWS dependencies)."""

from __future__ import annotations

import base64
import binascii
import gzip
import json
from dataclasses import dataclass, field

MARKETS = ("futures", "spot")
PRIMARY_TARGET = "ws_api"


@dataclass(frozen=True)
class MarketScore:
    market: str
    ok: bool
    best_ip: str | None = None
    p50_us: float | None = None
    p99_us: float | None = None
    worst_ip_p50_us: float | None = None
    samples: int = 0
    reason: str = ""


@dataclass
class InstanceScore:
    instance_id: str
    markets: dict[str, MarketScore] = field(default_factory=dict)
    passed: bool = False
    reason: str = ""

    def rank_key(self) -> tuple[float, float]:
        p50 = sum(score.p50_us or 0.0 for score in self.markets.values())
        p99 = sum(score.p99_us or 0.0 for score in self.markets.values())
        return (p50, p99)


def parse_report_lines(stdout: str) -> tuple[dict[str, dict], list[str]]:
    """Decode `REPORT <market> <base64(gzip(json))>` lines emitted by the SSM probe script."""
    reports: dict[str, dict] = {}
    errors: list[str] = []
    for line in stdout.splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) != 3 or parts[0] != "REPORT":
            continue
        market, payload = parts[1], parts[2]
        try:
            reports[market] = json.loads(gzip.decompress(base64.b64decode(payload, validate=True)))
        except (binascii.Error, OSError, EOFError, ValueError) as exc:
            errors.append(f"{market}: undecodable report ({exc.__class__.__name__})")
    return reports, errors


def score_market(report: dict, market: str, target: str = PRIMARY_TARGET,
                 min_samples: int = 50) -> MarketScore:
    entry = next((item for item in report.get("targets", []) if item.get("name") == target), None)
    if entry is None:
        return MarketScore(market, False, reason=f"target {target} missing")
    usable = []
    for ip in entry.get("ips", []):
        rtt = ip.get("rtt_us") or {}
        if rtt.get("count", 0) >= min_samples:
            usable.append((float(rtt["p50"]), float(rtt["p99"]), str(ip.get("ip")), int(rtt["count"])))
    if not usable:
        return MarketScore(market, False, reason=f"no {target} IP with >= {min_samples} samples")
    usable.sort()
    p50, p99, best_ip, count = usable[0]
    worst_p50 = max(item[0] for item in usable)
    return MarketScore(market, True, best_ip, p50, p99, worst_p50, count)


def evaluate(instance_id: str, reports: dict[str, dict], markets: list[str],
             thresholds: dict[str, float | None], min_samples: int = 50) -> InstanceScore:
    score = InstanceScore(instance_id)
    failures = []
    for market in markets:
        report = reports.get(market)
        if report is None:
            result = MarketScore(market, False, reason="no report")
        else:
            result = score_market(report, market, min_samples=min_samples)
        score.markets[market] = result
        limit = thresholds.get(market)
        if not result.ok:
            failures.append(f"{market}: {result.reason}")
        elif limit is not None and result.p50_us is not None and result.p50_us > limit:
            failures.append(f"{market}: p50 {result.p50_us:.1f}us > {limit:.1f}us")
    score.passed = not failures
    score.reason = "; ".join(failures)
    return score


def select_keep(scores: list[InstanceScore], keep: int) -> list[InstanceScore]:
    if keep <= 0:
        return []
    passing = sorted((score for score in scores if score.passed), key=InstanceScore.rank_key)
    return passing[:keep]
