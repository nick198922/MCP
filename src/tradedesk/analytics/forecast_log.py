"""Daily probabilistic range forecasts and their scoring (IDEA-20261018-assistant, "Журнал прогнозів").

Pure functions, no I/O; time is passed in. Once a day, for each asset, the forecast reads the Deribit smile of the
listed expiry closest to 7 days (4..11) and writes the RISK-NEUTRAL probability of four binary events on that
expiry's delivery price S_T (USD):

* ``above_spot``: S_T > S0, S0 = the Deribit index at issue;
* ``in_1sigma``: F e^(-sigma sqrt T) < S_T < F e^(+sigma sqrt T), sigma = ATM IV at the forward F, T = days / 365;
* ``in_pct_5``: 0.95 S0 < S_T < 1.05 S0;
* ``in_pct_10``: 0.90 S0 < S_T < 1.10 S0.

Probabilities come from the smile (Breeden-Litzenberger with the smile slope, `probability.prob_above`): they are
what the options market prices, not real-world odds. The volatility risk premium alone should make the bands hold
the outcome more often than priced; scoring the log is how that gap shows up, so it is a hypothesis under test.

Scores (decimals): Brier (p - o)^2 per forecast and its mean over a set; two references: a fixed one where the event
has a natural uninformed value (0.5 for ``above_spot``, 2 N(1) - 1 = 0.6827 for ``in_1sigma``) and the in-sample
climatology (base rate of each event kind in the scored set, a hindsight reference); skill = 1 - BS / BS_ref;
calibration in ten probability bins and the binned Murphy decomposition BS ~ reliability - resolution + uncertainty
(exact only when the forecasts inside a bin are equal). Forecasts issued on consecutive days for one expiry share the
outcome and are correlated: ``n_expiries`` counts the distinct (asset, expiry) outcomes, the honest sample size.

Beside each risk-neutral probability the row keeps a PHYSICAL baseline, the "RV30 model": the log return to the
delivery is normal with zero mean and the trailing 30-day zero-mean close-to-close realized vol (`vrp.realized_vol`,
Binance spot, annualised with 365), so P(lo < S_T < hi) = N(ln(hi/S0) / s) - N(ln(lo/S0) / s), s = RV30 sqrt(T). It
is a naive history-only forecast (no skew, no events; ``above_spot`` is always 0.5), there to answer one question:
do the options forecast the range better than recent realized vol does? `compare` scores both on the same settled
rows, paired, with the expiry as the unit of the sample.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .options import Smile, atm_iv, ncdf
from .probability import prob_above

KINDS = ("above_spot", "in_1sigma", "in_pct_5", "in_pct_10")
ONE_SIGMA = 2.0 * ncdf(1.0) - 1.0  # 0.6827: P(|Z| < 1)
REFERENCE: dict[str, float] = {"above_spot": 0.5, "in_1sigma": ONE_SIGMA}
TARGET_DTE, DTE_BAND = 7.0, (4.0, 11.0)
N_BINS = 10
RV_DAYS = 30  # trailing daily returns of the RV30 model
MODELS = ("risk_neutral", "rv30")
MIN_EXPIRIES = 30  # below this many settled expiries a comparison is descriptive only


def pick_expiry(
    expiries: Iterable[datetime], now: datetime, target: float = TARGET_DTE, band: tuple[float, float] = DTE_BAND
) -> datetime | None:
    """Listed expiry closest to ``target`` days with days to expiry inside ``band``; None when there is none."""
    cands = [e for e in expiries if band[0] <= (e - now).total_seconds() / 86400 <= band[1]]
    return min(cands, key=lambda e: abs((e - now).total_seconds() / 86400 - target)) if cands else None


def prob_between(smile: Smile, lo: float | None, hi: float | None) -> float:
    """Risk-neutral P(lo < S_T < hi) from the smile; ``None`` is an open side. Clipped to [0, 1]."""
    p_lo = 1.0 if lo is None else prob_above(smile, lo)
    p_hi = 0.0 if hi is None else prob_above(smile, hi)
    return min(1.0, max(0.0, p_lo - p_hi))


def event_bounds(kind: str, *, spot: float, forward: float, sigma: float, t_years: float) -> tuple[float | None, float | None]:
    """(lo, hi) in USD of the event ``kind``; ``None`` is an open side."""
    if kind == "above_spot":
        return spot, None
    if kind == "in_1sigma":
        w = sigma * math.sqrt(t_years)
        return forward * math.exp(-w), forward * math.exp(w)
    if kind.startswith("in_pct_"):
        x = float(kind[len("in_pct_") :]) / 100.0
        return spot * (1.0 - x), spot * (1.0 + x)
    raise ValueError(f"unknown forecast kind {kind!r}")


def prob_lognormal(lo: float | None, hi: float | None, *, spot: float, sigma: float, t_years: float) -> float:
    """P(lo < S_T < hi) when ln(S_T / spot) ~ N(0, sigma^2 T) (the RV30 model); ``None`` is an open side."""
    s = sigma * math.sqrt(t_years)
    if s <= 0 or spot <= 0:
        raise ValueError("prob_lognormal needs sigma > 0, t_years > 0 and spot > 0")
    p_lo = 1.0 if lo is None or lo <= 0 else 1.0 - ncdf(math.log(lo / spot) / s)
    p_hi = 0.0 if hi is None else 1.0 if hi <= 0 else 1.0 - ncdf(math.log(hi / spot) / s)
    return min(1.0, max(0.0, p_lo - p_hi))


def build_forecasts(
    smile: Smile, *, asset: str, spot: float, expiry: datetime, now: datetime, rv: float | None = None
) -> list[dict[str, Any]]:
    """One row per kind in :data:`KINDS` for the issue time ``now``; [] when the smile has no time or vol left.

    ``covered`` is True when every finite bound lies inside the quoted strikes (else the smile IV is flat-extrapolated).
    ``rv``: the trailing RV30 (decimal) for the RV30 model's ``prob_rv``; None leaves ``rv30`` / ``prob_rv`` empty.
    """
    sigma, t = atm_iv(smile), smile.T
    if t <= 0 or sigma <= 0 or spot <= 0:
        return []
    out = []
    for kind in KINDS:
        lo, hi = event_bounds(kind, spot=spot, forward=smile.forward, sigma=sigma, t_years=t)
        out.append(
            {
                "day": now.astimezone(UTC).date().isoformat(),
                "issued_ts": int(now.timestamp()),
                "asset": asset,
                "kind": kind,
                "expiry_ts": int(expiry.timestamp()),
                "spot": spot,
                "forward": smile.forward,
                "atm_iv": sigma,
                "t_years": t,
                "lo": lo,
                "hi": hi,
                "prob": prob_between(smile, lo, hi),
                "ref_prob": REFERENCE.get(kind),
                "covered": all(smile.covers(x) for x in (lo, hi) if x is not None),
                "rv30": rv if rv and rv > 0 else None,
                "prob_rv": prob_lognormal(lo, hi, spot=spot, sigma=rv, t_years=t) if rv and rv > 0 else None,
            }
        )
    return out


def outcome(lo: float | None, hi: float | None, s: float) -> int:
    """1 when lo < s < hi (an open side always passes), else 0."""
    return int((lo is None or s > lo) and (hi is None or s < hi))


def brier(p: float, o: int) -> float:
    return (float(p) - float(o)) ** 2


def _settled(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [x for x in rows if x.get("outcome") is not None and x.get("prob") is not None]


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def calibration(rows: Iterable[Mapping[str, Any]], n_bins: int = N_BINS) -> list[dict[str, Any]]:
    """Settled forecasts in ``n_bins`` equal probability bins (the last one includes 1.0); non-empty bins only.

    Per bin: ``n``, ``mean_prob`` (forecast) and ``freq`` (observed share of events that happened)."""
    bins: dict[int, list[Mapping[str, Any]]] = {}
    for x in _settled(rows):
        bins.setdefault(min(int(float(x["prob"]) * n_bins), n_bins - 1), []).append(x)
    return [
        {
            "lo": i / n_bins,
            "hi": (i + 1) / n_bins,
            "n": len(xs),
            "mean_prob": _mean([float(x["prob"]) for x in xs]),
            "freq": _mean([float(x["outcome"]) for x in xs]),
        }
        for i, xs in sorted(bins.items())
    ]


def score(rows: Iterable[Mapping[str, Any]], n_bins: int = N_BINS) -> dict[str, Any]:
    """Brier, references, skill, calibration and the binned Murphy decomposition of the settled rows in ``rows``."""
    xs = _settled(rows)
    n = len(xs)
    if n == 0:
        return {"n": 0}
    ps = [float(x["prob"]) for x in xs]
    os_ = [int(x["outcome"]) for x in xs]
    bs = _mean([brier(p, o) for p, o in zip(ps, os_, strict=True)])
    by_kind: dict[str, list[int]] = {}
    for x, o in zip(xs, os_, strict=True):
        by_kind.setdefault(str(x["kind"]), []).append(o)
    base = {k: _mean(v) for k, v in by_kind.items()}
    bs_clim = _mean([brier(base[str(x["kind"])], o) for x, o in zip(xs, os_, strict=True)])
    ref = [(p, float(x["ref_prob"]), o) for x, p, o in zip(xs, ps, os_, strict=True) if x.get("ref_prob") is not None]
    bins = calibration(xs, n_bins)
    obar = _mean([float(o) for o in os_])
    out: dict[str, Any] = {
        "n": n,
        "n_expiries": len({(x.get("asset"), x.get("expiry_ts")) for x in xs}),
        "mean_prob": _mean(ps),
        "base_rate": obar,
        "brier": bs,
        "brier_climatology": bs_clim,
        "skill_vs_climatology": 1.0 - bs / bs_clim if bs_clim > 0 else None,
        "n_ref": len(ref),
        "brier_on_ref": _mean([brier(p, o) for p, _, o in ref]) if ref else None,
        "brier_ref": _mean([brier(q, o) for _, q, o in ref]) if ref else None,
        "skill_vs_ref": None,
        "reliability": sum(b["n"] * (b["mean_prob"] - b["freq"]) ** 2 for b in bins) / n,
        "resolution": sum(b["n"] * (b["freq"] - obar) ** 2 for b in bins) / n,
        "uncertainty": obar * (1.0 - obar),
        "calibration": bins,
    }
    if ref and out["brier_ref"] > 0:
        out["skill_vs_ref"] = 1.0 - out["brier_on_ref"] / out["brier_ref"]
    return out


def iso_week(ts: int) -> str:
    """ISO week of a unix timestamp (UTC), e.g. '2026-W43'."""
    y, w, _ = datetime.fromtimestamp(int(ts), UTC).isocalendar()
    return f"{y}-W{w:02d}"


def weekly(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Settled forecasts grouped by the ISO week (UTC) of their expiry, oldest first.

    Per week: ``n``, ``n_expiries``, ``brier``, the fixed-reference comparison (``brier_on_ref`` / ``brier_ref`` /
    ``skill_vs_ref`` on ``n_ref`` rows) and the Brier per kind. No climatology: a week holds too few outcomes."""
    weeks: dict[str, list[Mapping[str, Any]]] = {}
    for x in _settled(rows):
        weeks.setdefault(iso_week(int(x["expiry_ts"])), []).append(x)
    out = []
    for wk, xs in sorted(weeks.items()):
        s = score(xs)
        kinds: dict[str, list[float]] = {}
        for x in xs:
            kinds.setdefault(str(x["kind"]), []).append(brier(float(x["prob"]), int(x["outcome"])))
        out.append(
            {
                "week": wk,
                "n": s["n"],
                "n_expiries": s["n_expiries"],
                "brier": s["brier"],
                "n_ref": s["n_ref"],
                "brier_on_ref": s["brier_on_ref"],
                "brier_ref": s["brier_ref"],
                "skill_vs_ref": s["skill_vs_ref"],
                "brier_by_kind": {k: _mean(v) for k, v in sorted(kinds.items())},
            }
        )
    return out


def as_model(rows: Iterable[Mapping[str, Any]], model: str) -> list[dict[str, Any]]:
    """The rows as forecasts of ``model`` (:data:`MODELS`): for ``rv30``, ``prob`` is the RV30 model's and rows
    without it are left out; outcome, references and expiry are shared, so `score` and `weekly` apply unchanged."""
    if model == "risk_neutral":
        return [dict(x) for x in rows]
    if model == "rv30":
        return [{**x, "prob": x["prob_rv"]} for x in rows if x.get("prob_rv") is not None]
    raise ValueError(f"unknown forecast model {model!r}")


def compare(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Paired Brier of the risk-neutral forecast against the RV30 model on the settled rows that have both.

    ``diff`` = BS(risk-neutral) - BS(RV30): negative means the options forecast better. Rows of one (asset, expiry)
    share the outcome, so the unit is the expiry: ``diff`` is the mean of the per-expiry mean differences, ``se`` their
    standard error and ``ci95`` = diff +- 1.96 se (a normal approximation, too narrow with few expiries; None below
    two). ``enough`` is False below :data:`MIN_EXPIRIES` expiries: the result is then descriptive, not a finding."""
    xs = [x for x in _settled(rows) if x.get("prob_rv") is not None]
    if not xs:
        return {"n": 0, "n_expiries": 0}
    clusters: dict[tuple[Any, Any], list[float]] = {}
    for x in xs:
        o = int(x["outcome"])
        clusters.setdefault((x.get("asset"), x.get("expiry_ts")), []).append(brier(float(x["prob"]), o) - brier(float(x["prob_rv"]), o))
    means = [_mean(v) for v in clusters.values()]
    k = len(means)
    diff = _mean(means)
    se = math.sqrt(sum((m - diff) ** 2 for m in means) / (k - 1) / k) if k >= 2 else None
    return {
        "n": len(xs),
        "n_expiries": k,
        "brier_risk_neutral": _mean([brier(float(x["prob"]), int(x["outcome"])) for x in xs]),
        "brier_rv30": _mean([brier(float(x["prob_rv"]), int(x["outcome"])) for x in xs]),
        "diff": diff,
        "se": se,
        "ci95": None if se is None else [diff - 1.96 * se, diff + 1.96 * se],
        "better": "risk_neutral" if diff < 0 else "rv30" if diff > 0 else "tie",
        "enough": k >= MIN_EXPIRIES,
    }
