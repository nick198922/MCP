"""Wikipedia attention: provider (Wikimedia pageviews + en.wikipedia page metadata) and the pure analytics."""

from __future__ import annotations

import json
import math
import random
import statistics
from pathlib import Path

import httpx
import pytest

from tradedesk.analytics.wiki_attention import (
    QUINTILE_Z_GAP,
    attention_shock,
    mde,
    ols_slope,
    placebo_sd,
    quintile_spread,
    rank_ic,
    rank_z,
    ranks,
    spike_days,
    window_mean,
    window_median,
    z_two_sided,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.wikimedia import (
    CONTACT_ENV,
    DEFAULT_CONTACT,
    USER_AGENT,
    Wikimedia,
    article_key,
    page_summary,
    pageviews_rows,
    user_agent,
)

FIX = Path(__file__).parent / "fixtures"
# Real answers fetched from the server on 2026-11-12 (scripts/verify_wiki_attention.py probe --save-fixtures):
# Pageviews per-article user/daily for "Solana (blockchain platform)" 2026-10-01..07, and the action API query for
# "Solana (blockchain)" (a redirect to page 68695418).
PV = json.loads((FIX / "wikimedia_pageviews_solana_2026-11-12.json").read_text())
PAGE = json.loads((FIX / "wikipedia_page_solana_2026-11-12.json").read_text())


def _wiki(handler_pv, handler_wiki=None) -> Wikimedia:
    return Wikimedia(
        Http(
            "wikimedia",
            "https://wikimedia.org/api/rest_v1",
            retries=0,
            headers={"User-Agent": USER_AGENT},
            transport=httpx.MockTransport(handler_pv),
        ),
        Http(
            "wikimedia:enwiki",
            "https://en.wikipedia.org",
            retries=0,
            headers={"User-Agent": USER_AGENT},
            transport=httpx.MockTransport(handler_wiki or handler_pv),
        ),
    )


# ---------------------------------------------------------------- provider


def test_article_key_escapes_reserved() -> None:
    assert article_key("Solana (blockchain platform)") == "Solana_%28blockchain_platform%29"
    assert article_key("AC/DC") == "AC%2FDC"
    assert article_key("$Trump") == "%24Trump"


def test_user_agent_has_contact(monkeypatch) -> None:
    monkeypatch.delenv(CONTACT_ENV, raising=False)
    assert DEFAULT_CONTACT in user_agent() and USER_AGENT == user_agent()
    monkeypatch.setenv(CONTACT_ENV, "https://example.org/research")
    assert "https://example.org/research" in user_agent() and DEFAULT_CONTACT not in user_agent()
    monkeypatch.setenv(CONTACT_ENV, "  ")
    assert DEFAULT_CONTACT in user_agent()


def test_pageviews_rows_from_fixture() -> None:
    rows = pageviews_rows(PV)
    assert rows == [
        ("20261001", 248),
        ("20261002", 246),
        ("20261003", 227),
        ("20261004", 234),
        ("20261005", 299),
        ("20261006", 343),
        ("20261007", 331),
    ]


def test_pageviews_rows_zero_day_absent_and_sorted() -> None:
    body = {"items": [{"timestamp": "2026100400", "views": 5}, {"timestamp": "2026100100", "views": 0}]}
    assert pageviews_rows(body) == [("20261001", 0), ("20261004", 5)]


@pytest.mark.parametrize("body", [{}, {"items": None}, {"items": [{"timestamp": "2026100100", "views": -1}]}, [1]])
def test_pageviews_rows_rejects_bad_shape(body) -> None:
    with pytest.raises(ProviderError):
        pageviews_rows(body)


def test_page_summary_from_fixture() -> None:
    s = page_summary(PAGE)
    assert s["pageid"] == 68695418
    assert s["title"] == "Solana (blockchain platform)"
    assert s["shortdesc"] == ""  # real answer: the local short description of this page is empty
    assert s["disambiguation"] is False
    assert s["redirected_from"] == ["Solana (blockchain)"]
    assert s["redirects"] == ["Solana (blockchain)"]
    assert s["lastrevid"] == 1384303981


def test_page_summary_missing_and_disambiguation() -> None:
    missing = {"query": {"pages": [{"ns": 0, "title": "Nope", "missing": True}]}}
    assert page_summary(missing)["pageid"] is None
    dis = {"query": {"pages": [{"pageid": 7, "ns": 0, "title": "Solana", "pageprops": {"disambiguation": ""}}]}}
    assert page_summary(dis)["disambiguation"] is True


async def test_provider_pageviews_path_and_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=PV)

    w = _wiki(handler)
    f = await w.pageviews_daily("Solana (blockchain platform)", "20261001", "20261004", agent="all-agents")
    assert f.data[0] == ("20261001", 248)
    assert (
        seen[0]
        .url.raw_path.decode()
        .endswith("/per-article/en.wikipedia/all-access/all-agents/Solana_%28blockchain_platform%29/daily/2026100100/2026100400")
    )
    assert seen[0].headers["user-agent"] == USER_AGENT
    await w.aclose()


async def test_provider_404_is_data_unavailable_and_bad_agent() -> None:
    w = _wiki(lambda req: httpx.Response(404, json={"title": "Not found."}))
    with pytest.raises(ProviderError) as e:
        await w.pageviews_daily("Nope", "20261001", "20261002")
    assert e.value.reason == "data_unavailable"
    with pytest.raises(ProviderError) as e2:
        await w.pageviews_daily("Nope", "20261001", "20261002", agent="robots")
    assert e2.value.reason == "config"


async def test_provider_403_robot_policy_is_auth() -> None:
    w = _wiki(lambda req: httpx.Response(403, text="Please respect our robot policy"))
    with pytest.raises(ProviderError) as e:
        await w.page("Bitcoin")
    assert e.value.reason == "auth"


async def test_provider_page_follows_redirect_continuation() -> None:
    calls: list[dict[str, str]] = []
    second = {
        "query": {
            "pages": [{"pageid": 1000001, "ns": 0, "title": "Solana (blockchain platform)", "redirects": [{"ns": 0, "title": "SOL coin"}]}]
        }
    }

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(dict(req.url.params))
        if "rdcontinue" in req.url.params:
            return httpx.Response(200, json=second)
        return httpx.Response(200, json={**PAGE, "continue": {"rdcontinue": "123|0", "continue": "||"}})

    w = _wiki(handler)
    f = await w.page("Solana (blockchain)")
    assert len(calls) == 2 and calls[1]["rdcontinue"] == "123|0"
    assert f.data["redirects"] == ["SOL coin", "Solana (blockchain)"]
    assert calls[0]["titles"] == "Solana (blockchain)" and calls[0]["redirects"] == "1"
    await w.aclose()


# ---------------------------------------------------------------- analytics


def test_window_mean_counts_absent_days_as_zero() -> None:
    v = {10: 7, 12: 14}
    assert window_mean(v, 10, 13) == pytest.approx(21 / 4)
    assert window_median(v, 10, 13) == pytest.approx(3.5)


def test_attention_shock_analytic() -> None:
    # 90 base days at 100 views, then 7 days at 299: A = ln(300 / 101)
    s = 200
    v = {d: 100 for d in range(s - 1 - 7 - 90 + 1, s - 1 - 7 + 1)}
    v.update({d: 299 for d in range(s - 7, s)})
    assert attention_shock(v, s) == pytest.approx(math.log(300 / 101))
    # lag 0: window s-6..s holds six 299-days and the absent day s; the base s-96..s-7 is 89 x 100 + one 299 (median 100)
    assert attention_shock(v, s, lag=0) == pytest.approx(math.log((299 * 6 + 0 + 1 * 7) / 7 / 101))
    # not enough history before the window
    assert attention_shock(v, s, first_day=s - 50) is None


def test_attention_shock_ignores_data_on_and_after_signal_day() -> None:
    rng = random.Random(3)
    s = 500
    v = {d: rng.randint(0, 1000) for d in range(300, 520)}
    a = attention_shock(v, s, lag=1)
    w = dict(v)
    for d in range(s, 520):  # change the signal day itself and everything after
        w[d] = 10**6
    assert attention_shock(w, s, lag=1) == a
    w[s - 1] = 10**6  # the last day the signal may use does move it
    assert attention_shock(w, s, lag=1) != a


def test_spike_days_flags_only_against_past_median() -> None:
    v = {d: 50 for d in range(0, 200)}
    v[150] = 50 * 21
    v[160] = 50 * 19
    assert spike_days(v) == [150]
    # a later burst does not change the flags before it
    w = dict(v)
    w.update({d: 10**6 for d in range(180, 200)})
    assert [d for d in spike_days(w) if d < 180] == [150]


def test_ranks_ties_and_rank_z() -> None:
    assert ranks([3.0, 1.0, 3.0, 2.0]) == [3.5, 1.0, 3.5, 2.0]
    z = rank_z([5.0, 1.0, 3.0])
    assert statistics.fmean(z) == pytest.approx(0.0, abs=1e-12)
    assert statistics.pstdev(z) == pytest.approx(1.0)
    assert rank_z([2.0, 2.0]) == [0.0, 0.0]


def test_slope_ic_spread_analytic() -> None:
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [0.02 * a + 0.01 for a in x]
    assert ols_slope(y, x) == pytest.approx(0.02)
    assert rank_ic(x, y) == pytest.approx(1.0)
    assert rank_ic(x, [-a for a in y]) == pytest.approx(-1.0)
    assert quintile_spread(x, y) == pytest.approx(y[-1] - y[0])
    xs = list(range(10))
    assert quintile_spread(xs, [float(a) for a in xs]) == pytest.approx(8.0)  # top 2 mean 8.5, bottom 2 mean 0.5


def test_placebo_sd_matches_null_theory() -> None:
    # Under a random signal the rank IC has SD ~ 1/sqrt(n-1); the slope on rank z has SD ~ sd(y)/sqrt(n).
    rng = random.Random(5)
    n = 60
    y = [rng.gauss(0.0, 0.08) for _ in range(n)]
    sl, ic, sp = placebo_sd(y, 3000, random.Random(9))
    assert ic == pytest.approx(1 / math.sqrt(n - 1), rel=0.08)
    assert sl == pytest.approx(statistics.pstdev(y) / math.sqrt(n - 1), rel=0.08)
    assert sp > 0


def test_z_and_mde() -> None:
    assert z_two_sided(0.05, 1) == pytest.approx(1.95996, abs=1e-4)
    assert z_two_sided(0.05, 2) == pytest.approx(2.24140, abs=1e-4)
    assert mde(0.01, m=2) == pytest.approx((2.24140 + 0.84162) * 0.01, abs=1e-5)
    assert QUINTILE_Z_GAP == pytest.approx(2.7713, abs=1e-4)
