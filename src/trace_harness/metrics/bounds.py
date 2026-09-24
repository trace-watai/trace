"""An exact one-sided upper bound for a failure rate seen on few trials.

``0 / 1`` reads as a zero rate and says almost nothing. The Clopper-Pearson
upper bound states how high the true rate could still be given what was
observed, which is the number a small denominator needs next to it. Computed
by bisection on the binomial CDF with ``math.comb`` so the package keeps its
single runtime dependency.
"""

from __future__ import annotations

import math


def binomial_cdf(k: int, n: int, p: float) -> float:
    """``P(X <= k)`` for ``X ~ Binomial(n, p)``.

    Terms are summed in log space. ``math.log`` accepts the exact integer from
    ``math.comb``, so a large ``n`` cannot overflow a float before the
    probability factors shrink it.
    """
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    total = sum(
        math.exp(math.log(math.comb(n, i)) + i * log_p + (n - i) * log_q) for i in range(k + 1)
    )
    return min(total, 1.0)


def clopper_pearson_upper(failures: int, trials: int, confidence: float = 0.95) -> float | None:
    """One-sided Clopper-Pearson upper bound on a rate after ``failures`` in ``trials``.

    The bound is the rate ``p`` at which ``failures`` or fewer would be seen
    with probability ``1 - confidence``. A true rate above it would make the
    observation rarer than that. ``0`` of ``1`` gives ``0.95``, and a clean
    run needs ``59`` trials before the bound drops under ``5%``.

    None for zero trials, since nothing was measured. The CDF falls
    monotonically in ``p``, so bisection converges; 100 halvings exceed
    double precision.
    """
    if not 0 <= failures <= trials:
        raise ValueError(f"need 0 <= failures <= trials, got {failures} of {trials}")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be strictly between 0 and 1, got {confidence}")
    if trials == 0:
        return None
    alpha = 1.0 - confidence
    low, high = 0.0, 1.0
    for _ in range(100):
        middle = (low + high) / 2
        if binomial_cdf(failures, trials, middle) > alpha:
            low = middle
        else:
            high = middle
    return high


#: Decimal places a bound is stored to. Printed as a percentage with two.
BOUND_PLACES = 4


def round_up(value: float, places: int = BOUND_PLACES) -> float:
    """``value`` rounded up to ``places`` decimals, for storing an upper bound.

    Plain rounding can store a bound below the exact one: ``0`` of ``59`` is
    0.04950761 and would be stored as 0.0495. A value within 1e-9 of a step,
    in units of that step, stays on the step, so float noise does not add a
    whole step: ``0.0051 * 10**4`` is 51.00000000000001, and a stored bound
    read back and rounded again keeps its value. That tolerance is far
    smaller than the bisection's own precision.
    """
    scale = 10**places
    return math.ceil(value * scale - 1e-9) / scale
