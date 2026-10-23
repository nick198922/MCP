"""Paired comparison of the demo trader's futures and option legs on the same signal (owner 23.10). Pure, no I/O.

A pair = one futures trade (``demo_trade``) and its option leg (``demo_option_leg``, ``pair_id`` = the futures link id),
both closed with an R. Futures R = P&L / risk at the stop (fees included); option R = P&L / (premium + fee paid at
entry). The question is whether "an option instead of a stop" does better on identical signals; on our data buying
options loses the variance risk premium on average, so this is a forward test, not a claimed edge.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .smart_money import mean_t_ci

MIN_PAIRS = 30  # below this the comparison is reported but marked "замало даних"
OPEN = ("unknown", "pending", "open", "closing")


def paired_report(futures: Iterable[Mapping[str, Any]], legs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """n pairs with both legs closed, mean R of each leg, the mean of (option R - futures R) with a 95 % Student-t CI,
    and how often the option outlived a futures stop-out and later closed at the target."""
    by_link = {t["link_id"]: t for t in futures}
    pairs, open_pairs, unpaired = [], 0, 0
    for leg in legs:
        f = by_link.get(leg["pair_id"])
        if f is None:
            unpaired += 1
            continue
        if leg["status"] in OPEN or f["status"] in ("unknown", "pending", "open"):
            open_pairs += 1
            continue
        if leg["status"] == "closed" and f["status"] == "closed" and leg.get("r_multiple") is not None and f.get("r_multiple") is not None:
            pairs.append((f, leg))
    rf = [float(f["r_multiple"]) for f, _ in pairs]
    ro = [float(leg["r_multiple"]) for _, leg in pairs]
    stopped = [(f, leg) for f, leg in pairs if f.get("exit_reason") == "stop"]
    survived = [
        (f, leg) for f, leg in stopped if leg.get("exit_reason") == "target" and (leg.get("closed_ts") or 0) >= (f.get("closed_ts") or 0)
    ]
    n = len(pairs)
    diff = mean_t_ci([o - f for o, f in zip(ro, rf, strict=True)])
    if n < MIN_PAIRS:
        verdict = f"замало даних (n < {MIN_PAIRS})"
    elif diff["lo"] is not None and diff["lo"] > 0:
        verdict = "опціон краще за ф'ючерс (95 % CI різниці вище нуля)"
    elif diff["hi"] is not None and diff["hi"] < 0:
        verdict = "ф'ючерс краще за опціон (95 % CI різниці нижче нуля)"
    else:
        verdict = "різниці не видно (95 % CI різниці містить нуль)"
    return {
        "n_pairs": n,
        "open_pairs": open_pairs,
        "legs_without_futures_row": unpaired,
        "mean_r_futures": sum(rf) / n if n else None,
        "mean_r_option": sum(ro) / n if n else None,
        "diff_option_minus_futures": diff,
        "futures_stopped": len(stopped),
        "option_target_after_futures_stop": len(survived),
        "share_option_survived_to_target": len(survived) / len(stopped) if stopped else None,
        "verdict": verdict,
    }
