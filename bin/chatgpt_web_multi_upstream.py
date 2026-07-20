from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


PLANNER_POLICIES = frozenset({"upstream-nonempty-prefix10", "strict-6-10"})
_JS_TRIM_RE = re.compile(
    r"^[\x09-\x0d\x20\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
    r"|[\x09-\x0d\x20\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+$"
)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def js_truthy_json(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return bool(value)
    return True


def _js_number_string(value: int | float) -> str:
    number = float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number == 0:
        return "0"
    if number.is_integer() and abs(number) < 1e21:
        return str(int(number))
    text = repr(number).lower()
    if "e" not in text:
        return text
    mantissa, exponent = text.split("e", 1)
    exponent_number = int(exponent)
    return f"{mantissa}e{'+' if exponent_number >= 0 else ''}{exponent_number}"


def js_string_json(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _js_number_string(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join("" if item is None else js_string_json(item) for item in value)
    return "[object Object]"


def js_json_stringify_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, separators=(",", ": "), allow_nan=False)


def js_trim(value: str) -> str:
    return _JS_TRIM_RE.sub("", value)


def js_utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


def math_imul_i32(left: int, right: int) -> int:
    unsigned = ((left & 0xFFFFFFFF) * (right & 0xFFFFFFFF)) & 0xFFFFFFFF
    return unsigned if unsigned < 0x80000000 else unsigned - 0x100000000


def adapt_planner_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    problem_analysis = payload.get("problem_analysis")
    approaches = payload.get("approaches")
    if not js_truthy_json(problem_analysis):
        raise ValueError("planner problem_analysis must be JavaScript-truthy")
    if not isinstance(approaches, list) or not approaches:
        raise ValueError("planner approaches must be a nonempty array")
    retained: list[dict[str, str]] = []
    for index, raw in enumerate(approaches[:10]):
        item = raw if isinstance(raw, Mapping) else {}
        default_name = f"Approach {index + 1}"
        retained.append(
            {
                "name": js_string_json(item.get("name") if js_truthy_json(item.get("name")) else default_name),
                "description": js_string_json(
                    item.get("description") if js_truthy_json(item.get("description")) else ""
                ),
                "methodology": js_string_json(
                    item.get("methodology") if js_truthy_json(item.get("methodology")) else ""
                ),
            }
        )
    return {
        "problem_analysis": js_string_json(problem_analysis),
        "approaches": retained,
        "observed_count": len(approaches),
        "retained_count": len(retained),
    }


def apply_planner_policy(payload: Mapping[str, Any], policy: str) -> dict[str, Any]:
    if policy not in PLANNER_POLICIES:
        raise ValueError(f"unknown planner policy: {policy}")
    approaches = payload.get("approaches")
    observed = len(approaches) if isinstance(approaches, list) else 0
    if policy == "strict-6-10" and not 6 <= observed <= 10:
        raise ValueError("strict-6-10 requires 6 through 10 observed approaches")
    adapted = adapt_planner_payload(payload)
    if policy == "upstream-nonempty-prefix10" and adapted["retained_count"] < 1:
        raise ValueError("upstream-nonempty-prefix10 requires at least one approach")
    return adapted


def _candidate_id(candidate: Mapping[str, Any]) -> int:
    raw = candidate.get("id")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("candidate id must be an integer")
    return raw


def stable_score_upstream(candidate: Mapping[str, Any], seed: int) -> int:
    candidate_id = _candidate_id(candidate)
    approach_name = candidate.get("approachName")
    if not js_truthy_json(approach_name):
        approach_name = ""
    content = js_string_json(candidate.get("content"))
    key = f"{seed}:{candidate_id}:{js_string_json(approach_name)}:{js_utf16_length(content)}"
    value = 2166136261
    encoded = key.encode("utf-16-le", errors="surrogatepass")
    for index in range(0, len(encoded), 2):
        code_unit = encoded[index] | (encoded[index + 1] << 8)
        value ^= code_unit
        value = math_imul_i32(value, 16777619)
    return value & 0xFFFFFFFF


def select_with_weights_upstream(
    candidates: Sequence[Mapping[str, Any]],
    *,
    outstanding_ids: Iterable[int] = (),
    seed: int,
    limit: int,
) -> list[Mapping[str, Any]]:
    outstanding = set(outstanding_ids)
    pool: list[Mapping[str, Any]] = []
    for candidate in candidates:
        pool.append(candidate)
        if _candidate_id(candidate) in outstanding:
            pool.append(candidate)
    ranked = sorted(pool, key=lambda item: stable_score_upstream(item, seed))
    selected: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    for candidate in ranked:
        candidate_id = _candidate_id(candidate)
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def build_merger_groups_upstream(
    candidates: Sequence[Mapping[str, Any]],
    *,
    outstanding_ids: Iterable[int] = (),
) -> list[list[Mapping[str, Any]]]:
    if not candidates:
        return []
    merger_count = min(8, len(candidates))
    group_size = min(3, len(candidates))
    return [
        select_with_weights_upstream(
            candidates,
            outstanding_ids=outstanding_ids,
            seed=seed,
            limit=group_size,
        )
        for seed in range(merger_count)
    ]


def normalize_judge_ids(raw_ids: Any, candidate_count: int) -> list[int]:
    values = raw_ids if isinstance(raw_ids, list) else []
    normalized: list[int] = []
    seen: set[int] = set()
    for raw in values:
        if isinstance(raw, bool):
            continue
        try:
            one_based = int(raw)
        except (TypeError, ValueError):
            continue
        zero_based = one_based - 1
        if one_based < 1 or zero_based >= candidate_count or zero_based in seen:
            continue
        seen.add(zero_based)
        normalized.append(zero_based)
    return normalized


def apply_judgment_upstream(
    candidates: Sequence[Mapping[str, Any]],
    judgment: Mapping[str, Any],
) -> dict[str, Any]:
    count = len(candidates)
    sufficient = bool(judgment.get("is_sufficient"))
    best_values = normalize_judge_ids([judgment.get("best_id")], count)
    outstanding = normalize_judge_ids(judgment.get("outstanding_ids"), count)
    inadequate = set(normalize_judge_ids(judgment.get("inadequate_ids"), count))
    if sufficient:
        selected = best_values[0] if best_values else (outstanding[0] if outstanding else 0)
        return {
            "is_sufficient": True,
            "best_index": selected if count else None,
            "candidates": [candidates[selected]] if count else [],
            "outstanding_ids": [0] if count else [],
        }
    survivor_indexes = [index for index in range(count) if index not in inadequate]
    if not survivor_indexes:
        survivor_indexes = list(range(count))
    remap = {old: new for new, old in enumerate(survivor_indexes)}
    remapped_outstanding = [remap[index] for index in outstanding if index in remap]
    if not remapped_outstanding:
        remapped_outstanding = list(range(len(survivor_indexes)))
    remapped_candidates: list[dict[str, Any]] = []
    for new_id, old_index in enumerate(survivor_indexes):
        candidate = dict(candidates[old_index])
        candidate["id"] = new_id
        remapped_candidates.append(candidate)
    return {
        "is_sufficient": False,
        "best_index": None,
        "candidates": remapped_candidates,
        "outstanding_ids": remapped_outstanding,
    }


def select_final_subset_upstream(
    candidates: Sequence[Mapping[str, Any]],
    outstanding_ids: Iterable[int],
) -> list[Mapping[str, Any]]:
    outstanding = set(outstanding_ids)
    if 0 < len(outstanding) < len(candidates):
        return [candidate for candidate in candidates if _candidate_id(candidate) in outstanding]
    return list(candidates)
