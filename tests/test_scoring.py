import pytest

from app.scoring import (
    contains_select_star,
    impact_level,
    normalize_sql,
    priority_score,
    query_fingerprint,
)


def test_fingerprint_ignores_numeric_literal_changes() -> None:
    first = query_fingerprint("SELECT name FROM `p.d.t` WHERE customer_id = 123")
    second = query_fingerprint("SELECT name FROM `p.d.t` WHERE customer_id = 456")
    assert first == second


def test_normalization_is_stable() -> None:
    assert normalize_sql(" SELECT  1 ") == normalize_sql("select 2")


@pytest.mark.parametrize(
    ("savings", "expected"),
    [(500, 1), (1_000, 2), (5_000, 3), (15_000, 4), (50_000, 5)],
)
def test_impact_level(savings: float, expected: int) -> None:
    assert impact_level(savings) == expected


def test_priority_rewards_savings_and_penalizes_risk() -> None:
    safe = priority_score(10_000, 0.8, 1, 1, effort=2, risk=1)
    risky = priority_score(10_000, 0.8, 1, 1, effort=2, risk=5)
    assert safe > risky


def test_priority_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError):
        priority_score(1_000, 1.5, 1, 1, 1, 1)


def test_select_star_detection() -> None:
    assert contains_select_star("SELECT * FROM `p.d.t`")
    assert contains_select_star("select t.* from `p.d.t` t")
    assert not contains_select_star("SELECT id, name FROM `p.d.t`")
