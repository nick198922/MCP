"""Tests of the 3.3 / 3.3б tag parser (tradedesk.analytics.call_labels) on real tag shapes from docs/calls/x/btc_charlie.csv."""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tradedesk.analytics.call_labels import parse_tag, quote_units, split_top
from tradedesk.analytics.calls import parse_calls

ROOT = Path(__file__).resolve().parents[1]


def ms(y, mo, d, h, mi) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


B18 = " · 3.3б (18.11, до цін; лише твіти рядка ≤ t0): "
B23 = " · 3.3б (23.11, до цін; лише твіти рядка ≤ t0): "
P23 = " · 3.3 (23.11, партія; до цін; лише твіти рядка ≤ t0): "


def test_split_top_ignores_separators_in_parens_and_quotes():
    s = "група ex ante (п. 3: 0.72 R; 1.54 R); з наміром; межовий за 3.3: альт. «a; b»; свінг"
    assert split_top(s) == ["група ex ante (п. 3: 0.72 R; 1.54 R)", "з наміром", "межовий за 3.3: альт. «a; b»", "свінг"]


def test_no_tag_is_none_and_bad_tag_raises():
    assert parse_tag("1234 (10:00): text without a tag") is None
    with pytest.raises(ValueError):
        parse_tag("x" + B23 + "група ex ante; строго власна; свінг")  # no t0
    with pytest.raises(ValueError):
        parse_tag("x" + B23 + "t0 01.01.2020 10:00 UTC (1212); група щось інше; свінг")


def test_plain_ex_ante_row_is_primary():
    lab = parse_tag("old note" + B23 + "t0 17.09.2026 16:25 UTC (2100622139594260573); група ex ante (п. 3: 0.10 R); "
                    "строго власна; свінг (графік 4h)")  # fmt: skip
    assert lab is not None
    assert (lab.tag, lab.t0_ms, lab.t0_id, lab.t0_moved) == ("3.3б", ms(2026, 9, 17, 16, 25), "2100622139594260573", False)
    assert (lab.group, lab.ownership, lab.scalp, lab.exclusion) == ("ex_ante", "strict", False, "")
    assert lab.primary and lab.idlist_variant and lab.border_variant and not lab.border


def test_moved_t0_takes_the_new_time():
    lab = parse_tag("n" + P23 + "t0 перенесено з 06:27 на 20.07.2020 06:33 UTC (вхід названо лише о 06:32, 1285100316908756992); "
                    "група показано вже в плюсі (п. 1: картка +1,220.27 %); строго власна; скальп (у тексті ≤ t0 «15m/30m charts»)")  # fmt: skip
    assert lab.t0_moved and lab.t0_ms == ms(2020, 7, 20, 6, 33) and lab.t0_id == "1285100316908756992"
    assert lab.group == "in_profit" and lab.scalp and not lab.primary


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ("виключення: умовний (пробій 712 сатоші; визначенню відповідає)", "conditional"),
        ("виключення: відновлений (перший id у note відсутній в архіві; твіт відомий лише з цитати)", "recovered"),
        ("виключення: збіг тікера", "collision"),
        ("виключення: TradFi (S&P 500, OANDA)", "tradfi"),
        ("виключення: одна угода з BC-2026-01-25-BTC-SHORT (лишається раніший рядок)", "same_trade"),
    ],
)
def test_exclusions_leave_the_primary_set(extra, code):
    lab = parse_tag("n" + B23 + f"t0 01.02.2020 10:00 UTC (1); група ex ante; з наміром; свінг; {extra}")
    assert lab.exclusion == code and not lab.primary and not lab.border_variant


def test_beam_idlist_and_limit_style():
    note = ("n" + B18 + "t0 24.04.2020 15:30 UTC (1253700000000000000); група ex ante (стоп-вхід не за ринком, 6.5 %); "
            "без названої власної позиції; свінг; не умовний (у тексті подію не названо: «Alerts set»; пробій — лише бокс); "
            "межовий за 3.3: альт. «умовний» (виключення); змінено 3.3б: було «умовний»; у будь-якому варіанті зі стилем "
            "`limit` — умовний (виключено); у обов'язковому варіанті «виключення за списком id 3.3» — виключено")  # fmt: skip
    lab = parse_tag(note)
    assert lab.ownership == "none" and lab.primary
    assert lab.idlist_out and not lab.idlist_variant
    assert lab.limit_conditional
    assert lab.border and not lab.border_variant  # alternative «умовний» puts it out


def test_border_rows_flip_by_their_alternative():
    unknown_in = parse_tag("n" + B18 + "t0 26.02.2020 09:00 UTC (1); група стан невідомий (п. 5); з наміром; свінг; "
                           "межовий за 3.3: альт. «ex ante» (ліміт нижче 8,000; у наборі)")  # fmt: skip
    assert not unknown_in.primary and unknown_in.border_variant
    own_only = parse_tag("n" + B18 + "t0 20.03.2020 10:00 UTC (1); група ex ante (п. 4); з наміром; свінг; "
                         "межовий за 3.3: альт. «строго власна»")  # fmt: skip
    assert own_only.primary and own_only.border_variant
    profit_in = parse_tag("n" + B23 + "t0 28.07.2026 06:38 UTC (1); група показано вже в плюсі (п. 3: 0.51 R); "
                          "власна позиція названа; свінг; межовий за 3.3: альт. ex ante (0.51 R з графіка)")  # fmt: skip
    assert profit_in.ownership == "named" and not profit_in.primary and profit_in.border_variant
    excluded = parse_tag("n" + B23 + "t0 27.04.2026 09:59 UTC (1); група стан невідомий (п. 5); власна позиція названа; "
                         "свінг; межовий за 3.3: альт. «показано вже в плюсі»; виключення: одна угода з X")  # fmt: skip
    assert not excluded.border_variant


def test_q2_template_rows():
    q2 = ("межовий за 3.3: шаблон Q-20261118-2 (відповідь 22.11, варіант 1: вхід за ринком + графік ≤ 10 хв до твіту; "
          "альт. (а), не рядок)")  # fmt: skip
    row = parse_tag("n" + B23 + f"t0 04.07.2020 07:36 UTC (1279318206365261825); група ex ante (шаблон Q-20261118-2: вхід "
                    f"за ринком, zone = мітка ціни графіка, m = 0); строго власна; свінг (графік 1D); {q2}")  # fmt: skip
    assert row.q2_template and row.primary and not row.q2_out and not row.border_variant
    out = parse_tag("n" + B23 + "t0 17.07.2020 19:12 UTC (1); група ex ante (шаблон Q-20261118-2: …) — поза первинним: "
                    "шаблон механічно не проходить (10 хв 05 с); строго власна; скальп (графік 60); межовий за 3.3: альт. "
                    "рядок у первинному (10 хв 05 с)")  # fmt: skip
    assert out.q2_out and not out.primary and out.border_variant and out.scalp


def test_last_tag_wins():
    note = "n" + B18 + "t0 01.01.2020 10:00 UTC (1); група ex ante; з наміром; свінг" + B23
    note += "t0 02.01.2020 11:00 UTC (1212675040000000000); група показано вже в плюсі (п. 1); власна позиція названа; свінг"
    lab = parse_tag(note)
    assert lab.group == "in_profit" and lab.t0_id == "1212675040000000000" and lab.t0_ms == ms(2020, 1, 2, 11, 0)


def test_quote_units():
    assert quote_units("binance:TOMO/BTC") == "BTC"
    assert quote_units("binance:DOT/USDT") == quote_units("") == "USD"


def test_border_ownership_takes_an_ownership_only_alternative():
    own_only = parse_tag("n" + B18 + "t0 20.03.2020 10:00 UTC (1); група ex ante (п. 4); з наміром; свінг; "
                         "межовий за 3.3: альт. «строго власна»")  # fmt: skip
    assert own_only.ownership == "intent" and own_only.border_ownership == "strict"
    group_alt = parse_tag("n" + B23 + "t0 04.07.2020 07:36 UTC (1); група ex ante; строго власна; свінг; межовий за 3.3: "
                          "шаблон Q-20261118-2 (альт. (а), не рядок)")  # fmt: skip
    assert group_alt.border_ownership == "strict"  # a group alternative never changes ownership
    plain = parse_tag("n" + B23 + "t0 01.02.2020 10:00 UTC (1); група ex ante; без названої власної позиції; свінг")
    assert plain.border_ownership == "none"


def test_every_btc_charlie_row_has_a_parsable_tag():
    calls, errors = parse_calls((ROOT / "docs/calls/x/btc_charlie.csv").read_text(encoding="utf-8"))
    assert not errors
    labs = {c.id: parse_tag(c.note) for c in calls}
    assert all(lab is not None for lab in labs.values())
    assert labs["BC-2020-05-11-BTC-SHORT"].group == "in_profit"
    assert labs["BC-2025-12-16-PIPPIN-LONG"].group == "in_profit"  # fixed by registration 3.3
    assert labs["BC-2020-04-24-BEAM-LONG"].limit_conditional


# --- the committed markup (card IDEA-20261113-x-traders, 3.3б): frozen before any price -------------------------------

sys.path.insert(0, str(ROOT / "scripts"))
import verify_btc_charlie_eval as bce  # noqa: E402

SNOWFLAKE_EPOCH_MS = 1288834974657


def test_t0_id_is_the_t0_tweet_for_every_row():
    """The tweet time of ``t0_id`` lies in the t0 minute (t0 − 1 min for a moved t0: the entry tweet, t0 = it + 1 min)."""
    rows, errors = bce.load_rows()
    assert not errors and rows
    bad = []
    for r in rows:
        if not r.lab.t0_id:
            bad.append((r.id, "empty"))
            continue
        minute = ((int(r.lab.t0_id) >> 22) + SNOWFLAKE_EPOCH_MS) // 60_000
        ok = {r.lab.t0_ms // 60_000} | ({r.lab.t0_ms // 60_000 - 1} if r.lab.t0_moved else set())
        if minute not in ok:
            bad.append((r.id, minute * 60_000 - r.lab.t0_ms))
    assert not bad


def test_committed_markup_equals_the_generator_output():
    rows, _ = bce.load_rows()
    assert bce.MARKUP.read_text(encoding="utf-8") == bce.markup_text([r for r in rows if r.sample])


def test_main_sample_is_the_census_prefix_only():
    """The 3.3 census stopped on five quarters (23.11); later 2020-2022 batches (2021-Q2) stay outside the markup."""
    assert bce.sample_of(date(2020, 7, 6)) == "main"
    assert bce.sample_of(date(2021, 11, 23)) == "main"
    assert bce.sample_of(date(2021, 5, 12)) == ""
    assert bce.sample_of(date(2022, 6, 1)) == ""
    assert bce.sample_of(date(2025, 12, 16)) == "combo"
    rows, _ = bce.load_rows()
    q2 = [r for r in rows if r.id.startswith("BC-2021-0") and r.call.day.month in (4, 5, 6)]
    assert q2 and not any(r.sample for r in q2)


def test_markup_counters_are_pinned():
    """Counters of the committed markup (3.3б, step 2 after review B). A label edit after the first price would move
    them: change these numbers only together with a labelled correction in the card."""
    rows, _ = bce.load_rows()
    assert bce.summary(rows, "main") == {
        "rows": 101, "primary": 64, "idlist_variant": 61, "border_variant": 41, "without_border_rows": 34,
        "without_q2_template": 48, "q2_template_rows": 17, "q2_template_in_primary": 16, "strict_primary": 36,
        "strict_border_variant": 19, "strict_without_q2_template": 20, "scalps_primary": 11, "btc_units_primary": 25,
        "shorts_primary": 10, "weeks_primary": 27,
    }  # fmt: skip
    assert bce.summary(rows, "combo") == {
        "rows": 77, "primary": 17, "idlist_variant": 17, "border_variant": 14, "without_border_rows": 13,
        "without_q2_template": 16, "q2_template_rows": 1, "q2_template_in_primary": 1, "strict_primary": 4,
        "strict_border_variant": 3, "strict_without_q2_template": 3, "scalps_primary": 3, "btc_units_primary": 0,
        "shorts_primary": 5, "weeks_primary": 15,
    }  # fmt: skip


# --- stage 0 availability (review B 3.3б, p. 6): holes inside t0 … t0 + 336 h --------------------------------------

H = bce.H
T0 = ms(2020, 3, 2, 10, 15)


def hourly(lo: int, hi: int, skip: range | tuple = ()) -> list:
    base = T0 // H * H
    return [bce.Bar(base + k * H, 1.0, 1.0, 1.0, 1.0) for k in range(lo, hi + 1) if k not in skip]


def test_full_window_is_ok_and_has_no_holes():
    bars = hourly(-5, 400)
    assert bce.holes(bars, T0, 336 * H) == ([], 0)
    assert bce.availability(bars, T0, 336 * H) == "ok"


def test_short_holes_count_but_do_not_fail():
    bars = hourly(-5, 400, skip=(10, 11, 12, 100, 101, 102, 103))  # 3 missing (not a hole) + 4 missing (a hole)
    assert bce.holes(bars, T0, 336 * H) == ([4], 7)
    assert bce.availability(bars, T0, 336 * H) == "ok"


def test_a_hole_over_a_day_or_too_many_missing_hours_is_short():
    assert bce.availability(hourly(-5, 400, skip=range(50, 63)), T0, 336 * H) == "short"  # one hole of 13 h
    assert bce.availability(hourly(-5, 400, skip=range(50, 62)), T0, 336 * H) == "ok"  # 12 h: at the threshold
    spread = tuple(j for k in range(20, 320, 15) for j in (k, k + 1))  # 20 holes of 2 h: 40 h > 5 % of 336 h
    assert bce.holes(hourly(-5, 400, skip=spread), T0, 336 * H) == ([], 40)
    assert bce.availability(hourly(-5, 400, skip=spread), T0, 336 * H) == "short"


def test_missing_and_early_end():
    assert bce.availability([], T0, 336 * H) == "missing"
    assert bce.availability(hourly(3, 400), T0, 336 * H) == "missing"  # starts more than 2 h after t0
    assert bce.availability(hourly(-5, 300), T0, 336 * H) == "short"


def test_a_hole_at_the_start_of_the_window_is_seen():
    # review B 3.3в, p. 1: a bar 1 h before t0, then nothing until t0's hour + 40 h -> hours 0 … 39 of the grid missing
    bars = hourly(-5, -1) + hourly(40, 400)
    assert bce.holes(bars, T0, 336 * H) == ([40], 40)
    assert bce.availability(bars, T0, 336 * H) == "missing"  # the first bar at or after t0's hour is > t0 + 2 h
    bars = hourly(-5, 0) + hourly(41, 400)  # t0's hour present, then 40 h missing: a hole, 'short'
    assert bce.holes(bars, T0, 336 * H) == ([40], 40)
    assert bce.availability(bars, T0, 336 * H) == "short"


def test_a_hole_at_the_end_of_the_window_is_seen():
    # bars up to t0's hour + 236 h, the next one at + 400 h: grid hours 237 … 336 (100 h) missing, though bars resume
    bars = hourly(-5, 236) + hourly(400, 450)
    assert bce.holes(bars, T0, 336 * H) == ([100], 100)
    assert bce.availability(bars, T0, 336 * H) == "short"


def test_registered_variant_compositions():
    # card 3.3в, p. 18 (review B 3.3в, p. 2): fixed lists, all inside the primary set; cluster heads stay in 'one idea'
    rows, _ = bce.load_rows()
    prim = {r.id for r in rows if r.lab.primary and r.sample}
    members = {m for ms in bce.ONE_IDEA.values() for m in ms}
    assert set(bce.STRICT_BOUNDARY_OUT) <= prim and members <= prim and set(bce.ONE_IDEA) <= prim
    assert not set(bce.ONE_IDEA) & members
    by_id = {r.id: r for r in rows}
    assert all(by_id[i].lab.exclusion == "conditional" and not by_id[i].lab.primary for i in bce.CONFIRMED_ENTRY)
    main = bce.variant_ids(rows, "main")
    assert (len(main["strict_boundary"]), len(main["one_idea"]), len(main["confirmed_entry"])) == (61, 61, 65)
    combo = bce.variant_ids(rows, "combo")
    assert (len(combo["strict_boundary"]), len(combo["one_idea"]), len(combo["confirmed_entry"])) == (15, 17, 17)
    assert bce.confirmed_t0("BC-2020-04-24-ALGO-LONG") == ms(2020, 4, 25, 7, 14)


def test_drift_coverage_counts_hours_with_a_full_7_day_hold():
    times = {b.t for b in hourly(-30 * 24, 30 * 24 + 167)}
    assert bce.drift_coverage(times, T0) == 1.0
    half = {b.t for b in hourly(-30 * 24, 167)}  # holds only for hours up to t0's hour
    assert bce.drift_coverage(half, T0) == pytest.approx((30 * 24 + 1) / (60 * 24 + 1))
    assert bce.drift_coverage(set(), T0) == 0.0
