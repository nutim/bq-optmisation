"""Deterministic scoring and query normalization utilities."""

import hashlib
import math
import re

try:
    import sqlglot
except ImportError:  # Enables lightweight unit testing before dependency installation.
    sqlglot = None  # type: ignore[assignment]


def normalize_sql(sql: str) -> str:
    """Return stable BigQuery SQL for grouping; fall back safely on parse errors."""
    if sqlglot is not None:
        try:
            collapsed = sqlglot.parse_one(sql, read="bigquery").sql(
                dialect="bigquery", normalize=True, pretty=False
            )
        except Exception:
            collapsed = sql
    else:
        collapsed = sql
    collapsed = re.sub(r"'([^']|'')*'", "?", collapsed)
    collapsed = re.sub(r"\b\d+(?:\.\d+)?\b", "?", collapsed)
    return " ".join(collapsed.lower().split())


def query_fingerprint(sql: str) -> str:
    return hashlib.sha256(normalize_sql(sql).encode("utf-8")).hexdigest()


def priority_score(
    expected_monthly_savings: float,
    confidence: float,
    recurrence: float,
    business_weight: float,
    effort: int,
    risk: int,
) -> float:
    """Calculate an explainable, bounded priority score."""
    if expected_monthly_savings < 0 or not 0 <= confidence <= 1:
        raise ValueError("invalid savings or confidence")
    denominator = max(1, effort) * max(1, risk)
    raw = (
        expected_monthly_savings
        * confidence
        * max(0, recurrence)
        * max(0, business_weight)
        / denominator
    )
    return round(math.log1p(raw), 4)


def impact_level(expected_monthly_savings: float) -> int:
    if expected_monthly_savings >= 50_000:
        return 5
    if expected_monthly_savings >= 15_000:
        return 4
    if expected_monthly_savings >= 5_000:
        return 3
    if expected_monthly_savings >= 1_000:
        return 2
    return 1


def contains_select_star(sql: str) -> bool:
    return bool(re.search(r"(?is)\bselect\s+(?:\w+\.)?\*", sql))
