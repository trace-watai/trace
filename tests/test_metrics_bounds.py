"""The one-sided Clopper-Pearson bound reported next to small denominators.

Known values are the published one-sided 95% upper limits, so a change to the
solver shows up as a wrong number rather than a quietly shifted one.
"""

from __future__ import annotations

import pytest

from trace_harness.metrics.bounds import binomial_cdf, clopper_pearson_upper


@pytest.mark.parametrize(
    ("failures", "trials", "expected"),
    [(0, 1, 0.95), (0, 40, 0.0722), (1, 40, 0.1132), (3, 10, 0.6066), (0, 2, 0.7764)],
)
def test_known_upper_bounds(failures: int, trials: int, expected: float) -> None:
    assert clopper_pearson_upper(failures, trials) == pytest.approx(expected, abs=5e-5)


def test_a_clean_record_needs_59_trials_to_bound_under_five_percent() -> None:
    assert clopper_pearson_upper(0, 59) < 0.05
    assert clopper_pearson_upper(0, 58) > 0.05


@pytest.mark.parametrize(("failures", "trials"), [(0, 1), (1, 40), (5, 100), (2, 7)])
def test_the_bound_is_where_the_observation_has_five_percent_probability(
    failures: int, trials: int
) -> None:
    bound = clopper_pearson_upper(failures, trials)
    assert bound is not None
    assert binomial_cdf(failures, trials, bound) == pytest.approx(0.05, abs=1e-9)


def test_every_trial_failing_bounds_at_one() -> None:
    assert clopper_pearson_upper(3, 3) == 1.0


def test_zero_trials_is_not_measured() -> None:
    """A bound over nothing would be a number with no observation behind it."""
    assert clopper_pearson_upper(0, 0) is None


def test_large_denominators_do_not_overflow() -> None:
    assert clopper_pearson_upper(0, 3000) == pytest.approx(0.000998, abs=1e-6)
    assert clopper_pearson_upper(50, 3000) == pytest.approx(0.02105, abs=1e-5)


@pytest.mark.parametrize(
    ("failures", "trials", "confidence"),
    [(-1, 1, 0.95), (2, 1, 0.95), (0, -1, 0.95), (0, 1, 0.0), (0, 1, 1.0)],
)
def test_impossible_inputs_are_refused(failures: int, trials: int, confidence: float) -> None:
    with pytest.raises(ValueError):
        clopper_pearson_upper(failures, trials, confidence)


def test_binomial_cdf_edges() -> None:
    assert binomial_cdf(0, 5, 0.0) == 1.0
    assert binomial_cdf(4, 5, 1.0) == 0.0
    assert binomial_cdf(5, 5, 1.0) == 1.0
    assert binomial_cdf(1, 2, 0.5) == pytest.approx(0.75)
