"""Invariant 1: `finite_json` returns a bool and never raises, however deep the value is nested (item 375).

Before: only ValueError / TypeError were caught, so a value nested deeper than the json encoder's recursion limit
raised RecursionError out of drop_corrupt_rows / clean_tags. The limit differs between versions: 5000 levels raise on
Python 3.11 but encode on 3.12.3 and 3.13.16; 100000 levels raise on all three (checked 13.11). So the deep cases use
both depths and put a NaN at the bottom: the answer is False whether the encoder gives up on depth or reaches the NaN.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from tradedesk.tools._util import finite_json

DEPTHS = [5000, 100_000]


def _deep_list(n: int) -> list[Any]:
    root: list[Any] = []
    cur = root
    for _ in range(n):
        nxt: list[Any] = []
        cur.append(nxt)
        cur = nxt
    cur.append(math.nan)
    return root


def _deep_dict(n: int) -> dict[str, Any]:
    root: dict[str, Any] = {}
    cur = root
    for _ in range(n):
        nxt: dict[str, Any] = {}
        cur["k"] = nxt
        cur = nxt
    cur["v"] = math.nan
    return root


@pytest.mark.parametrize("n", DEPTHS)
@pytest.mark.parametrize("make", [_deep_list, _deep_dict], ids=["list", "dict"])
def test_deep_nesting_is_false_not_raised(make, n):
    assert finite_json(make(n)) is False


@pytest.mark.parametrize("x", [math.nan, [math.inf], {"a": [1, {"b": -math.inf}]}], ids=["nan", "inf_list", "nested_minus_inf"])
def test_non_finite_is_false(x):
    assert finite_json(x) is False


def test_blob_is_false():
    assert finite_json({"meta": b"\x00"}) is False


@pytest.mark.parametrize("x", [None, 1, 1.5, "inf", "NaN", [], {}, {"a": [1, "x", None, {"b": 2.5}]}], ids=str)
def test_plain_json_is_true(x):
    assert finite_json(x) is True
