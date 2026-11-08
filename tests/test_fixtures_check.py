"""scripts/fixtures_check.py (item 146 of docs/ideas/IDEA-20261102-standing-improvements.md): fixture provenance marks."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fixtures_check as fc  # noqa: E402

DATED = "deribit_sample_2026-10-14.json"
FIELD = '{\n  "_source": "GET https://example/api, fetched by hand",\n  "result": []\n}\n'
CAPTURED = '{"captured_at": "2026-10-25T10:00Z", "data": [1]}'
NESTED_ONLY = '{"data": [{"source": "news site", "title": "x"}]}'
HTML_COMMENT = "<!-- source: https://t.me/s/channel, 2026-10-23 -->\n<html></html>\n"
NOTHING = '{"name": "Fear and Greed Index", "data": [{"value": "27"}]}'


def test_name_with_date():
    assert fc.name_has_date(DATED)
    assert fc.name_has_date("doubletop/ours__x_1d_2026-10-18.json")
    assert not fc.name_has_date("binance_vision_ethusdt_dwt_2026-10.json")  # month only
    assert not fc.name_has_date("sample_2026-13-01.json")  # not a calendar date
    assert fc.marks(DATED, NOTHING, "") == ["name-date"]


def test_field_and_comment():
    assert fc.marks("a.json", FIELD, "") == ["field"]
    assert fc.marks("b.json", CAPTURED, "") == ["field"]
    assert fc.marks("c.json", '{"_captured_utc": "2026-10-28"}', "") == ["field"]
    # a "source" key inside the payload is data, not provenance
    assert fc.marks("d.json", NESTED_ONLY, "") == []
    assert fc.marks("e.html", HTML_COMMENT, "") == ["comment"]
    assert fc.marks("f.csv", "# captured: 2026-10-01 by curl\nts,px\n", "") == ["comment"]


def test_nothing_and_readme():
    assert fc.marks("fear_greed_sample.json", NOTHING, "") == []
    readme = "| `fear_greed_sample.json` | 2026-10-10 | d3ff031c |\n| tests/fixtures/doubletop/x.json | ... |\n"
    assert fc.marks("fear_greed_sample.json", NOTHING, readme) == ["readme"]
    assert fc.marks("doubletop/x.json", None, readme) == ["readme"]
    # part of a longer name is not a mention
    assert fc.marks("greed_sample.json", NOTHING, readme) == []
    assert fc.marks("x.json", NOTHING, readme) == []
    # binary content is judged by name and README only
    assert fc.marks("blob.zip", None, "") == []


def test_check_and_render():
    fixtures = {
        DATED: NOTHING,
        "a.json": FIELD,
        "nothing.json": NOTHING,
        "listed.json": NOTHING,
        "archive.zip": None,
    }
    r = fc.check(fixtures, "see `listed.json`.")
    assert r.failed
    assert (r.files, r.binary) == (5, 1)
    assert r.unmarked == ["archive.zip", "nothing.json"]
    assert r.readme_only == ["listed.json"]
    assert r.by_mark == {"name-date": 1, "field": 1, "comment": 0, "readme": 1}
    text = fc.render(r)
    assert "no provenance mark (2):\n  archive.zip\n  nothing.json" in text
    assert not fc.check({DATED: None}, "").failed


def test_load_fixtures_skips_readme(tmp_path):
    d = tmp_path / "tests" / "fixtures"
    (d / "sub").mkdir(parents=True)
    (d / "README.md").write_text("`plain.json`\n", encoding="utf-8")
    (d / "plain.json").write_text(NOTHING, encoding="utf-8")
    (d / "sub" / "z_2026-01-02.zip").write_bytes(b"PK\x03\x04\x00\xff")
    files, readme = fc.load_fixtures(d)
    assert files == {"plain.json": NOTHING, "sub/z_2026-01-02.zip": None}
    out = tmp_path / "out" / "fixtures.txt"
    assert fc.main(["--root", str(tmp_path), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("tests/fixtures: 2 files (1 binary)")


def test_main_on_repository_writes_out(tmp_path):
    out = tmp_path / "fixtures.txt"
    fc.main(["--root", str(ROOT), "--out", str(out)])
    assert out.read_text(encoding="utf-8").startswith("tests/fixtures:")
