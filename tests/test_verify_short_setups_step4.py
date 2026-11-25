"""scripts/verify_short_setups_step4.py: counts print no R, the report runs, the run refuses before its time."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_short_setups_step4 as M  # noqa: E402

DAY = M.DAY_MS
SB = M.ms(M.SIGNAL_BEFORE)
# the registered hashes of the card (pinned here: rewriting the tracked manifest must fail this test file)
SHA_S38 = "26e14e15602bebf53ffc6195dd056cd72a7764a4a8e795261861277c85273b0e"
SHA_S39 = "9cabf0c9eccd9b93906934bb61bd7a54f24aa806a7afd7f44bbc957244b6b78b"
REAL_MAIN_BYTES = M.main_manifest_bytes  # before the autouse patch


@pytest.fixture(autouse=True)
def _main_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read the real origin/main: the manifest there is «unknown» unless a test says otherwise."""
    monkeypatch.setattr(M, "main_manifest_bytes", lambda *_a, **_k: None)


def _db(tmp_path: Path, rows: list[tuple]) -> str:
    db = tmp_path / "j.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE demo_trade (link_id TEXT, setup TEXT, status TEXT, signal_ts INTEGER, closed_ts INTEGER, timeframe TEXT, "
        "version TEXT, r_multiple REAL, side TEXT, symbol TEXT, opened_ts INTEGER)"
    )
    con.executemany("INSERT INTO demo_trade VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return str(db)


def test_counts_prints_counts_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    t0 = SB - 10 * DAY
    db = _db(
        tmp_path,
        [
            ("a", "S38", "closed", t0, t0 + DAY, "4h", "v5", 1.75, "short", "AAA/USDT:USDT", t0),
            ("b", "S38", "missed", t0, None, "4h", "v5", None, "short", "BBB/USDT:USDT", None),
            ("td-S39-s-4h-BSV-1794614400", "S39", "closed", t0, M.ms("2026-11-23T00:00"), "4h", None, -0.625, "short", "BSV/USDT:USDT", t0),
            ("c", "S39", "open", SB, None, "4h", "v5", None, "short", "CCC/USDT:USDT", SB),  # at the boundary: outside
        ],
    )
    assert M.counts(db) == 0
    out = capsys.readouterr().out
    assert "S38: closed 1 (r_multiple present 1), other terminal {'missed': 1}, blocking 0" in out and "DIFFERS" in out
    assert "S39: closed 1" in out and "path mismatch 1: BSV" in out and "R-new (closed ≥ 2026-11-22T13:15) 1" in out
    assert "manifest S38: n_closed 1, other terminal 1, sha256 " in out and "manifest.json: DIFFERS" in out
    assert "1.75" not in out and "0.625" not in out  # no R printed


def _row(i: int, setup: str, r_demo: float, pl: float, r_new: bool | None = None) -> dict:
    p = {"coin": "X", "1.0": {"r": pl, "status": "ok"}, "1.5": {"r": pl - 0.05, "status": "ok"}}
    return {
        "link_id": f"l{i}", "setup": setup, "symbol": f"C{i % 7}/USDT:USDT", "side": "short", "tf": "4h", "signal_ts": i * DAY,
        "day": i, "version": "v5" if i % 5 else None, "r_demo": r_demo, "status": "ok",
        "sim_signal": {"1.0": {"r": r_demo, "status": "ok"}, "1.5": {"r": r_demo - 0.05, "status": "ok"}},
        "placebo": [p] * 10, "placebo_ng": [p] * 10, "pool_n": 90, "pool_gold": 0,
        "r_new": i % 3 == 0 if r_new is None else r_new,
        "delta_new": i % 2 == 0, "pnl_mismatch": False, "path_mismatch": i == 4, "btc_regime": (i % 3) - 1,
    }  # fmt: skip


BLIND = {"perps": 3, "n_missing": 0, "btc_missing": 0, "run_wall_ms": 0, "registration_main_ms": M.ss.S39_BLIND_DEADLINE_MS - 1}
S39_SMALL = [_row(100 + i, "S39", 0.5, 0.0) for i in range(12)]  # 12 trades: ◯


def _txt(rows: list[dict], meta: dict | None = None, v2: list[dict] | None = None) -> str:
    return "\n".join(M.report(rows, BLIND if meta is None else meta, 200 * DAY, v2))


def test_report_smoke() -> None:
    rows = [_row(i, "S38", (-1) ** i * 1.0, 0.1) for i in range(32)] + S39_SMALL
    txt = _txt(rows, {**BLIND, "registration_main_ms": None})
    assert "Вердикти: S38" in txt and "Holm m 2" in txt and "Статус картки за правилом: inconclusive" in txt
    assert "S39 ◯" in txt  # 12 trades: below 30
    assert "S39 лише облік" in txt and "Легенда" in txt and "без поправки на два сетапи" in txt
    assert "рядки v2 не передано" in txt and "до воріт MDE80 ≤ 0.30R" in txt
    for label in ("без топ-5 Δ", "без найкращого дня", "BTC тренд −1", "R-нові (у жодному звіті)", "S38 + S39 pooled", "лише v5"):
        assert label in txt
    assert "край виконання: R демо − модель монети" in txt


def test_report_confirms_a_strong_excess() -> None:
    rows = [_row(i, "S38", 0.5 + 0.1 * (-1) ** i, 0.0) for i in range(40)] + S39_SMALL
    txt = _txt(rows)
    assert "Вердикти: S38 ✓" in txt and "S39 ◯" in txt and "S39 сліпий щодо Δ" in txt
    assert "Статус картки за правилом: confirmed" in txt
    # the same S38 with S39 for the record only: S38 alone still confirms
    assert "Статус картки за правилом: confirmed" in _txt(rows, {**BLIND, "registration_main_ms": M.ss.S39_BLIND_DEADLINE_MS})


def test_report_rejects_a_strong_deficit() -> None:
    rows = [_row(i, "S38", -0.5 + 0.1 * (-1) ** i, 0.0) for i in range(40)] + S39_SMALL
    txt = _txt(rows)
    assert "Вердикти: S38 ✗" in txt and "Статус картки за правилом: inconclusive" in txt  # S39 ◯


def test_report_new_trades_of_the_other_sign_give_approx() -> None:
    # Δ +0.8 on old trades, −0.1 on the 14 «R-new» ones: significant and powered, but R-new < 0 -> ≈, not ✓
    rows = [_row(i, "S38", -0.1 if i % 3 == 0 else 0.8, 0.0) for i in range(40)] + S39_SMALL
    txt = _txt(rows)
    assert "Вердикти: S38 ≈" in txt and "R-new n 14 Δ -0.100" in txt


def test_report_prints_the_v2_s39_delta_beside() -> None:
    rows = [_row(i, "S38", (-1) ** i * 1.0, 0.1) for i in range(32)] + S39_SMALL
    v2 = [_row(100 + i, "S39", 0.5, 0.1) for i in range(12)]
    txt = _txt(rows, v2=v2)
    assert "S39 крок 4 (основна)" in txt and "S39 v2 28.11 (облік)" in txt and "Δ S39 крок 4 − v2 +0.100" in txt


def test_latest_ts_and_git_failure(tmp_path: Path) -> None:
    assert M.latest_ts("1795900000\n1795800000\n") == 1_795_900_000_000  # a later rewrite of the manifest moves the time
    assert M.latest_ts("") is None
    assert M.registration_main_ms(tmp_path / "no-repo") is None  # git fails: unknown, S39 for the record only


def test_main_manifest_bytes_git_failure(tmp_path: Path) -> None:
    assert REAL_MAIN_BYTES(tmp_path / "no-repo") is None  # git fails: unknown
    assert REAL_MAIN_BYTES(path="docs/verification/data/no-such-file.json") is None  # not on origin/main: unknown


def test_run_refuses_before_registered_time(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _db(tmp_path, [])
    assert M.run(db, tmp_path, M.NOW_MS - 1, tmp_path / "o") == 2
    assert "refused" in capsys.readouterr().out and not (tmp_path / "o").exists()
    assert M.run(db, tmp_path, 4_102_444_800_000, tmp_path / "o") == 2  # in the future


T0 = SB - 10 * DAY
CLOSED = [
    ("a", "S38", "closed", T0, T0 + DAY, "4h", "v5", 1.75, "short", "AAA/USDT:USDT", T0),
    ("b", "S38", "missed", T0, None, "4h", "v5", None, "short", "BBB/USDT:USDT", None),
    ("z", "S39", "closed", T0, T0 + 2 * DAY, "4h", "v5", -0.625, "short", "ZZZ/USDT:USDT", T0),
]


def _manifest(tmp_path: Path, db: str) -> Path:
    closed, other, _ = M.P.load_window(db, M.SETUPS, SB)
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(M.manifest_doc(closed, other)), encoding="utf-8")
    return p


class _Reached(Exception):
    pass


def _no_network(*_a: object, **_k: object) -> None:
    raise _Reached


def test_run_refuses_a_non_terminal_row_in_the_window(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _db(tmp_path, [*CLOSED, ("o", "S39", "open", T0 + DAY, None, "4h", "v5", None, "short", "OOO/USDT:USDT", T0 + DAY)])
    assert M.run(db, tmp_path, M.NOW_MS, tmp_path / "o", manifest=_manifest(tmp_path, db), wall_ms=M.NOW_MS + 1) == 2
    out = capsys.readouterr().out
    assert "refused: the window holds rows that are not terminal" in out and not (tmp_path / "o").exists()


@pytest.mark.parametrize(
    "change",
    [
        ("UPDATE demo_trade SET r_multiple = 1.5 WHERE link_id = 'a'"),  # R changed, same rows and counts
        ("UPDATE demo_trade SET closed_ts = closed_ts + 1 WHERE link_id = 'z'"),
        ("UPDATE demo_trade SET link_id = 'a2' WHERE link_id = 'a'"),
        ("UPDATE demo_trade SET status = 'rejected' WHERE link_id = 'b'"),
    ],
)
def test_run_refuses_a_sample_that_differs_from_the_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    db = _db(tmp_path, CLOSED)
    man = _manifest(tmp_path, db)
    con = sqlite3.connect(db)
    con.execute(change)
    con.commit()
    con.close()
    monkeypatch.setattr(M.P, "collect", _no_network)
    assert M.run(db, tmp_path, M.NOW_MS, tmp_path / "o", manifest=man, wall_ms=M.NOW_MS + 1) == 2
    out = capsys.readouterr().out
    assert "refused: the sample differs from the registered manifest" in out and not (tmp_path / "o").exists()
    assert "1.5" not in out and "1.75" not in out and "0.625" not in out  # no R printed


def test_run_passes_the_manifest_check_on_the_registered_sample(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path, CLOSED)
    man = _manifest(tmp_path, db)
    monkeypatch.setattr(M.P, "collect", _no_network)
    monkeypatch.setattr(M, "registration_main_ms", lambda *_a, **_k: None)
    with pytest.raises(_Reached):  # got past every refusal to the placebo fetch
        M.run(db, tmp_path, M.NOW_MS, tmp_path / "o", manifest=man, wall_ms=M.NOW_MS + 1)
    out = capsys.readouterr().out
    assert "manifest manifest.json: match" in out and "vs origin/main: unknown" in out
    assert "registration on origin/main None" in out  # unknown on main: S39 for the record only, as an unknown time
    assert M.run(db, tmp_path, M.NOW_MS, tmp_path / "o", manifest=tmp_path / "absent.json", wall_ms=M.NOW_MS + 1) == 2


def test_run_checks_the_manifest_against_origin_main(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path, CLOSED)
    man = _manifest(tmp_path, db)
    monkeypatch.setattr(M.P, "collect", _no_network)
    monkeypatch.setattr(M, "registration_main_ms", lambda *_a, **_k: M.ss.S39_BLIND_DEADLINE_MS - 1)
    monkeypatch.setattr(M, "main_manifest_bytes", lambda *_a, **_k: man.read_bytes() + b" ")  # rewritten on a branch
    assert M.run(db, tmp_path, M.NOW_MS, tmp_path / "o", manifest=man, wall_ms=M.NOW_MS + 1) == 2
    out = capsys.readouterr().out
    assert "differs from origin/main:" in out and not (tmp_path / "o").exists() and "1.75" not in out
    monkeypatch.setattr(M, "main_manifest_bytes", lambda *_a, **_k: man.read_bytes())
    with pytest.raises(_Reached):
        M.run(db, tmp_path, M.NOW_MS, tmp_path / "o", manifest=man, wall_ms=M.NOW_MS + 1)
    out = capsys.readouterr().out
    assert "vs origin/main: match" in out and f"registration on origin/main {M.ss.S39_BLIND_DEADLINE_MS - 1}" in out


def test_report_prints_the_manifest_line() -> None:
    rows = [_row(i, "S38", (-1) ** i * 1.0, 0.1) for i in range(32)] + S39_SMALL
    man = {"path": "/x/m.json", "sha256": {"S38": SHA_S38, "S39": SHA_S39}, "sample": "match", "main": "match"}
    txt = _txt(rows, {**BLIND, "manifest": man})
    assert f"Маніфест /x/m.json: sha256 S38 {SHA_S38}, S39 {SHA_S39}; вибірка: match; проти origin/main: match" in txt
    assert "Маніфест —: sha256 ; вибірка: —; проти origin/main: —" in _txt(rows)  # an old meta without the record


def test_tracked_manifest_is_well_formed() -> None:
    doc = json.loads(M.MANIFEST.read_text(encoding="utf-8"))
    for s, n, sha in (("S38", 55, SHA_S38), ("S39", 37, SHA_S39)):
        m = doc["setups"][s]
        assert m["n_closed"] == n == len(m["link_ids"]) and m["link_ids"] == sorted(m["link_ids"])
        assert m["sha256"] == sha  # the registered hash of the card: a rewritten manifest fails here
    assert len(doc["setups"]["S38"]["other_terminal"]) == 3 and doc["signal_before_utc"] == M.SIGNAL_BEFORE
