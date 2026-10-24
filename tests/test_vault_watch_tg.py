"""The monthly vault watch in scripts/vault_watch_tg.py (fake API, no network)."""

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import vault_watch_tg as w  # noqa: E402

D = 86_400_000
S = w.START_MS


def details(pnl, av=1000.0, closed=False):
    """allTime series one day apart around START: constant capital, cumulative PnL as given."""
    ts = [S - D + i * D for i in range(len(pnl))]
    block = {"accountValueHistory": [[t, str(av)] for t in ts], "pnlHistory": [[t, str(p)] for t, p in zip(ts, pnl, strict=True)]}
    return {"portfolio": [["allTime", block]], "isClosed": closed}


def test_since_counts_only_after_the_start():
    r = w.since(details([0, 50, 150, 100]), S)  # +50 before START is not ours; then +100, −50 on 1000
    assert abs(r["ret"] - (1.05 * 1.1 * (1 - 50 / 1000) / 1.05 - 1)) < 1e-9
    assert abs(r["max_dd"] - 0.05) < 1e-9 and r["tvl"] == 1000.0


def test_since_without_history_is_a_reason():
    assert w.since({"portfolio": []}, S) == {"error": "немає історії"}


def test_btc_return_from_the_last_close_before_the_start():
    candles = [{"t": S - D, "c": "100"}, {"t": S, "c": "105"}, {"t": S + D, "c": "110"}]
    assert abs(w.btc_return(candles, S) - 0.10) < 1e-12
    assert w.btc_return([{"t": S, "c": "1"}], S) is None


def test_verdict_needs_return_over_beta_and_a_smaller_fall():
    ok = {"ret": 0.10, "max_dd": 0.05, "closed": False}
    assert w.verdict(ok, 0.5, 0.12, 0.10) == "тримає"
    assert w.verdict(ok, 1.0, 0.12, 0.20) == "не тримає"
    assert w.verdict({**ok, "max_dd": 0.2}, 0.5, 0.12, 0.0) == "не тримає"
    assert w.verdict({**ok, "closed": True}, 0.5, 0.12, 0.0) == "закрито"


def test_collect_survives_a_failing_api_and_escapes_names():
    def post(body):
        if body["type"] == "candleSnapshot":
            raise OSError("down")
        if body["vaultAddress"] == w.VAULTS[0][1]:
            raise TimeoutError
        return details([0, 0, 10, 20])

    rows, btc = w.collect(post, now_ms=S + 3 * D)
    assert btc is None and rows[0][3] == {"error": "API: TimeoutError"}
    text = w.build([("<x>", 0.5, 0.1, rows[1][3]), *rows], btc, datetime.fromtimestamp((S + 3 * D) / 1000, UTC))
    assert "&lt;x&gt;" in text and "API: TimeoutError" in text and "3 дн." in text and "BTC 1x за цей час: —" in text
