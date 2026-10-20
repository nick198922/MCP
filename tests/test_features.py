"""Feature switches from deploy/features.env (src/tradedesk/features.py)."""

from pathlib import Path

from tradedesk import features


def test_switches_of_read_only_jobs_are_accepted():
    ok, rejected = features.parse(
        "# comment\n\nTRADEDESK_PAPER_CARRY_ENABLED=1\nTRADEDESK_ASSISTANT_SNAPSHOT_SECONDS=600\n"
        "TRADEDESK_COLLECT_OHLCV_HIST_MARKETS=binance:BTC/USDT:USDT\nTRADEDESK_PM_GAP_ENABLED=1\n"
    )
    assert ok == {
        "TRADEDESK_PAPER_CARRY_ENABLED": "1",
        "TRADEDESK_ASSISTANT_SNAPSHOT_SECONDS": "600",
        "TRADEDESK_COLLECT_OHLCV_HIST_MARKETS": "binance:BTC/USDT:USDT",
        "TRADEDESK_PM_GAP_ENABLED": "1",
    }
    assert rejected == []


def test_secrets_bind_address_and_paths_are_ignored():
    for line in (
        "TRADEDESK_HOST=0.0.0.0",
        "TRADEDESK_PORT=80",
        "TRADEDESK_TRANSPORT=stdio",
        "TRADEDESK_HTTP_BEARER_TOKEN=x",
        "TRADEDESK_DB_PATH=/tmp/x",
        "TRADEDESK_ASSISTANT_SNAPSHOT_PATH=/etc/x",
        "TRADEDESK_ALERTS_RULES_PATH=/x",
        "TRADEDESK_COLLECT_KEY_ENABLED=1",
        "TRADEDESK_EXECUTION_ENABLED=1",
        "COINALYZE_API_KEY=x",
        "TELEGRAM_BOT_TOKEN=x",
        "TRADEDESK_PAPER_CARRY_ENABLED=1; rm -rf /",
        "TRADEDESK_PAPER_CARRY_SECONDS=../../etc",
        "TRADEDESK_PAPER_CARRY_ENABLED",
    ):
        ok, rejected = features.parse(line)
        assert ok == {} and len(rejected) == 1, line


def test_the_owners_environment_wins(tmp_path: Path):
    f = tmp_path / "features.env"
    f.write_text("TRADEDESK_PAPER_CARRY_ENABLED=1\nTRADEDESK_COLLECT_SUPPLY_ENABLED=1\nTRADEDESK_HOST=0.0.0.0\n")
    env = {"TRADEDESK_COLLECT_SUPPLY_ENABLED": "0", "TRADEDESK_HOST": "127.0.0.1"}
    assert features.load(f, env) == {"TRADEDESK_PAPER_CARRY_ENABLED": "1"}
    assert env == {
        "TRADEDESK_COLLECT_SUPPLY_ENABLED": "0",
        "TRADEDESK_HOST": "127.0.0.1",
        "TRADEDESK_PAPER_CARRY_ENABLED": "1",
    }
    assert features.load(tmp_path / "absent.env", env) == {}


def test_the_shipped_file_is_valid():
    ok, rejected = features.parse(features.DEFAULT_PATH.read_text())
    assert rejected == []
