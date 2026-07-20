from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "chatgpt_web_multi_upstream.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_web_multi_upstream_test", MODULE_PATH)
assert SPEC and SPEC.loader
UPSTREAM = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = UPSTREAM
SPEC.loader.exec_module(UPSTREAM)


def test_js_json_boundary_semantics() -> None:
    assert not UPSTREAM.js_truthy_json(None)
    assert not UPSTREAM.js_truthy_json(0)
    assert UPSTREAM.js_truthy_json([])
    assert UPSTREAM.js_truthy_json({})
    assert UPSTREAM.js_string_json(None) == "null"
    assert UPSTREAM.js_string_json([1, None, True]) == "1,,true"
    assert UPSTREAM.js_string_json({}) == "[object Object]"
    assert UPSTREAM.js_trim("\ufeff\xa0 x \u3000") == "x"
    assert UPSTREAM.js_utf16_length("a😀") == 3
    assert UPSTREAM.math_imul_i32(0x7FFFFFFF, 2) == -2


@pytest.mark.parametrize(
    ("count", "upstream_ok", "strict_ok", "retained"),
    [
        (0, False, False, 0),
        (1, True, False, 1),
        (5, True, False, 5),
        (6, True, True, 6),
        (10, True, True, 10),
        (11, True, False, 10),
    ],
)
def test_planner_cardinality_policies(
    count: int,
    upstream_ok: bool,
    strict_ok: bool,
    retained: int,
) -> None:
    payload = {
        "problem_analysis": " ",
        "approaches": [{"name": "", "description": "", "methodology": ""} for _ in range(count)],
    }
    for policy, expected in (
        ("upstream-nonempty-prefix10", upstream_ok),
        ("strict-6-10", strict_ok),
    ):
        if not expected:
            with pytest.raises(ValueError):
                UPSTREAM.apply_planner_policy(payload, policy)
        else:
            adapted = UPSTREAM.apply_planner_policy(payload, policy)
            assert adapted["retained_count"] == retained
            assert adapted["approaches"][0]["name"] == "Approach 1"


def test_five_candidate_golden_groups_and_utf16_score() -> None:
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "web_multi_upstream_v1.json").read_text(encoding="utf-8")
    )
    candidates = [
        {"id": index, "approachName": "", "content": "x"}
        for index in range(5)
    ]
    groups = UPSTREAM.build_merger_groups_upstream(candidates)
    assert [[item["id"] for item in group] for group in groups] == fixture["five_candidate_group_ids"]
    ascii_score = UPSTREAM.stable_score_upstream(
        {"id": 0, "approachName": "", "content": "aa"},
        0,
    )
    emoji_score = UPSTREAM.stable_score_upstream(
        {"id": 0, "approachName": "", "content": "😀"},
        0,
    )
    assert emoji_score == ascii_score


def test_outstanding_weighting_is_stable_and_deduplicated() -> None:
    candidates = [
        {"id": index, "approachName": "", "content": "x"}
        for index in range(5)
    ]
    selected = UPSTREAM.select_with_weights_upstream(
        candidates,
        outstanding_ids={1, 3},
        seed=0,
        limit=5,
    )
    assert len(selected) == 5
    assert len({item["id"] for item in selected}) == 5


def test_judge_normalization_filter_remap_and_all_inadequate_restore() -> None:
    candidates = [
        {"id": index, "approachName": f"a{index}", "content": f"c{index}"}
        for index in range(3)
    ]
    transitioned = UPSTREAM.apply_judgment_upstream(
        candidates,
        {
            "is_sufficient": False,
            "outstanding_ids": [3, 3, 99, "x"],
            "inadequate_ids": [1],
        },
    )
    assert [item["id"] for item in transitioned["candidates"]] == [0, 1]
    assert [item["approachName"] for item in transitioned["candidates"]] == ["a1", "a2"]
    assert transitioned["outstanding_ids"] == [1]

    restored = UPSTREAM.apply_judgment_upstream(
        candidates,
        {
            "is_sufficient": False,
            "outstanding_ids": [],
            "inadequate_ids": [1, 2, 3],
        },
    )
    assert [item["approachName"] for item in restored["candidates"]] == ["a0", "a1", "a2"]
    assert restored["outstanding_ids"] == [0, 1, 2]


def test_final_subset_only_uses_nonempty_proper_subset() -> None:
    candidates = [{"id": index} for index in range(4)]
    assert [item["id"] for item in UPSTREAM.select_final_subset_upstream(candidates, [1, 3])] == [1, 3]
    assert [item["id"] for item in UPSTREAM.select_final_subset_upstream(candidates, [])] == [0, 1, 2, 3]
    assert [item["id"] for item in UPSTREAM.select_final_subset_upstream(candidates, range(4))] == [0, 1, 2, 3]


def test_canonical_json_rejects_nan_and_has_no_newline() -> None:
    assert UPSTREAM.canonical_json_bytes({"가": 1, "a": 2}) == '{"a":2,"가":1}'.encode("utf-8")
    with pytest.raises(ValueError):
        UPSTREAM.canonical_json_bytes({"bad": float("nan")})
