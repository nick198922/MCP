"""The Binance liquidation stream uses the USD-M market-streams path (the legacy /ws/ path went silent in 2026)."""

from tradedesk.providers.liquidations import BINANCE_WS_URL


def test_binance_force_order_uses_market_path():
    assert BINANCE_WS_URL == "wss://fstream.binance.com/market/ws/!forceOrder@arr"
