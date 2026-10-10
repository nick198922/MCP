"""Risk-neutral probabilities from the option smile + barrier (touch) probabilities.

All probabilities here are RISK-NEUTRAL (what the options market prices), not real-world forecasts.
"""

from __future__ import annotations

import math

import numpy as np

from .options import Smile, d1d2, ncdf, npdf


def prob_above(smile: Smile, K: float) -> float:
    """P(S_T > K) = -dC/dK = N(d2) - vega * dsigma/dK  (Breeden-Litzenberger with the smile slope)."""
    F, T = smile.forward, smile.T
    sigma = smile.iv(K)
    d1, d2 = d1d2(F, K, T, sigma)
    h = 0.005
    slope = (smile.iv(K * (1 + h)) - smile.iv(K * (1 - h))) / (K * 2 * h)
    vega_unit = F * npdf(d1) * math.sqrt(T)  # per 1.00 of vol
    return float(min(1.0, max(0.0, ncdf(d2) - vega_unit * slope)))


def prob_touch(F: float, barrier: float, sigma: float, T: float) -> float:
    """P(price touches `barrier` at any time before T), continuous monitoring, driftless forward
    measure (log-drift -sigma^2/2), constant `sigma`."""
    if T <= 0 or sigma <= 0:
        return 1.0 if barrier == F else 0.0
    s = sigma * math.sqrt(T)
    mu = -0.5 * sigma * sigma
    if barrier == F:
        return 1.0
    if barrier > F:
        b = math.log(barrier / F)
        return float(ncdf((-b + mu * T) / s) + math.exp(2 * mu * b / (sigma * sigma)) * ncdf((-b - mu * T) / s))
    a = math.log(F / barrier)
    return float(ncdf((-a - mu * T) / s) + math.exp(-2 * mu * a / (sigma * sigma)) * ncdf((-a + mu * T) / s))


def mc_touch(
    F: float, barrier: float, sigma: float, T: float, *, n_paths: int = 40000, steps: int = 400, seed: int = 7
) -> float:
    """Monte Carlo check of `prob_touch` (Broadie-Glasserman barrier shift for discrete monitoring)."""
    rng = np.random.default_rng(seed)
    dt = T / steps
    shift = math.exp(0.5826 * sigma * math.sqrt(dt))
    b = barrier / shift if barrier > F else barrier * shift
    z = rng.standard_normal((n_paths, steps))
    logp = math.log(F) + np.cumsum((-0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * z, axis=1)
    if barrier > F:
        hit = logp.max(axis=1) >= math.log(b)
    else:
        hit = logp.min(axis=1) <= math.log(b)
    return float(hit.mean())
