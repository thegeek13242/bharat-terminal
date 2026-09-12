"""Canonical hashing: the duplicate detector and the reproducibility key."""
from __future__ import annotations

from bharat_terminal.factory.dsl.hashing import result_hash, spec_hash, sweep_point_hash
from tests.factory.conftest import make_spec


def test_hash_is_stable_across_calls():
    assert spec_hash(make_spec()) == spec_hash(make_spec())


def test_prose_and_identity_do_not_change_the_hash():
    """Two agents describing the same experiment differently must collide."""
    a = make_spec()
    b = make_spec(
        id="H-999999",
        version=7,
        family="some_other_family",
        question="RQ-000099",
        parents=[],
        claim={
            "description": "A completely different way of wording the very same test.",
            "mechanism": "Different prose describing the same underlying mechanism entirely.",
            "falsified_if": "Something else written here.",
        },
    )
    assert spec_hash(a) == spec_hash(b)


def test_mechanics_changes_do_change_the_hash():
    base = spec_hash(make_spec())
    assert spec_hash(make_spec(exit={"holding_days": [5, 10], "stop": "none"},
                               statistics={"primary_holding_days": 10})) != base
    assert spec_hash(make_spec(universe={"min_median_traded_value_cr": 10.0})) != base
    assert spec_hash(make_spec(benchmark="universe_median")) != base
    assert spec_hash(make_spec(costs={"multiplier": 2.0})) != base
    assert spec_hash(make_spec(entry={"rule": "next_session_close"})) != base


def test_sweep_point_hash_is_order_independent():
    assert sweep_point_hash({"a": 1, "b": 2}) == sweep_point_hash({"b": 2, "a": 1})
    assert sweep_point_hash({"a": 1}) != sweep_point_hash({"a": 2})


def test_result_hash_tolerates_float_noise_but_not_real_change():
    base = {"mean": 0.0142, "cohorts": [0.01, 0.02], "n": 1742}
    noise = {"mean": 0.0142 + 1e-13, "cohorts": [0.01, 0.02], "n": 1742}
    real = {"mean": 0.0143, "cohorts": [0.01, 0.02], "n": 1742}
    assert result_hash(base) == result_hash(noise)
    assert result_hash(base) != result_hash(real)


def test_result_hash_is_key_order_independent():
    assert result_hash({"a": 1, "b": 2}) == result_hash({"b": 2, "a": 1})
