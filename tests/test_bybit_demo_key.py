"""scripts/server/bybit_demo_key.py: signing and the key-settings checks (no network)."""

from __future__ import annotations

import hashlib
import hmac
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "server"))
import bybit_demo_key as bdk  # noqa: E402

OK = {
    "readOnly": 0,
    "permissions": {"ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"], "Wallet": []},
    "ips": ["169.58.169.184"],
}


def test_sign_is_v5_hmac_of_ts_key_window_query():
    want = hmac.new(b"sec", b"1700000000000KEY10000accountType=UNIFIED", hashlib.sha256).hexdigest()
    assert bdk.sign("sec", "1700000000000", "KEY", "accountType=UNIFIED") == want


def test_a_trading_key_bound_to_the_server_passes():
    assert bdk.key_problems(OK) == []


def test_each_bad_setting_is_reported():
    assert any("read-only" in p for p in bdk.key_problems({**OK, "readOnly": 1}))
    assert any("no trading permission" in p for p in bdk.key_problems({**OK, "permissions": {"Spot": []}}))
    assert any("Wallet" in p for p in bdk.key_problems({**OK, "permissions": {**OK["permissions"], "Wallet": ["AccountTransfer"]}}))
    assert any("none" in p for p in bdk.key_problems({**OK, "ips": ["*"][:0]}))
    assert any("1.2.3.4" in p for p in bdk.key_problems({**OK, "ips": ["169.58.169.184", "1.2.3.4"]}))
