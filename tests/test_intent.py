# -*- coding: utf-8 -*-
"""用例意图只有一份：词表、判定、以及所有需要意图的地方都从 trustharness.intent 取。

这组测试守的是三件真实发生过的事：

1. 词表被抄成三份后各自漂移——「安全测试」在自检里算负向，分诊不认识；
   平台认 正向/成功，harness 不认
2. 调用方拿原始 data_type 直接查词表、绕过 resolve_intent——boundary
   落进「意图未知」，断言重写只剩「状态码 < 500」
3. 落库的响应快照是 JSON 字符串时，执行有效性把每条都当成没拿到响应
"""
import enum
import json

import pytest

from trustharness import intent as intent_mod
from trustharness.conventions import build_assertions, default_convention, rewrite_assertions
from trustharness.evidence import (
    AUTH_REJECTED,
    OK,
    REACHED,
    UNUSABLE,
    environment_usable,
    scan_results,
)
from trustharness.intent import (
    BEHAVIOR_TO_INTENT,
    KNOWN_INTENTS,
    NEGATIVE_INTENTS,
    POSITIVE_INTENTS,
    is_positive,
    resolve_intent,
)


# ── 1. 只有一份 ─────────────────────────────────────────
@pytest.mark.parametrize("module_name", ["trustharness.selfcheck", "trustharness.triage"])
@pytest.mark.parametrize("name", ["NEGATIVE_INTENTS", "POSITIVE_INTENTS",
                                  "AMBIGUOUS_INTENTS", "BEHAVIOR_TO_INTENT",
                                  "resolve_intent"])
def test_modules_share_the_one_vocabulary(module_name, name):
    """不是「内容相同」，是同一个对象——内容相同的两份迟早会漂开。"""
    import importlib

    module = importlib.import_module(module_name)
    assert getattr(module, name) is getattr(intent_mod, name), (
        f"{module_name}.{name} 不是 trustharness.intent 里那一份")


def test_security_testing_is_negative_in_triage_too():
    """漂移回归：「安全测试」以前只在自检那份词表里。

    一条安全测试被接口以业务码拒绝，是接口在做正确的事，错在断言；
    分诊不认识这个意图时，只能判「证据不足」丢给人工。
    """
    from trustharness.triage import BAD_ASSERTION, triage

    verdict = triage(
        status="failed",
        data_type="安全测试",
        response_snapshot={"status_code": 200, "body": {"code": 10001, "message": "非法字符"}},
        assertion_details=[{"type": "json_path", "path": "code", "expected": 0,
                            "actual": 10001, "passed": False}],
    )
    assert verdict["category"] == BAD_ASSERTION, verdict


# ── 2. 词表合并了平台那份 ───────────────────────────────
@pytest.mark.parametrize("data_type", ["正向", "正常", "normal"])
def test_platform_positive_synonyms_are_known(data_type):
    assert is_positive(data_type)


@pytest.mark.parametrize("behavior", ["成功", "ok", "2xx", "success"])
def test_platform_success_behaviors_are_known(behavior):
    # data_type 故意给负向：expected_behavior 必须压过它才算认得
    assert resolve_intent("invalid", behavior) == "valid"


@pytest.mark.parametrize("data_type", ["反向", "负向", "异常"])
def test_negative_counterparts_exist(data_type):
    """认「正向」却不认「反向」，表格导入的用例只能用上一半。"""
    assert resolve_intent(data_type) in NEGATIVE_INTENTS


def test_error_behavior_is_known():
    """demo_routes 写入 expected_behavior="error"，以前词表里没有。"""
    assert resolve_intent("valid", "error") == "invalid"


def test_values_are_normalized():
    class Behavior(enum.Enum):
        SUCCESS = "success"

    assert resolve_intent(" VALID ") == "valid"
    assert resolve_intent("boundary", "Client_Error") == "invalid"
    # 枚举取 .value；str(枚举) 会得到 "Behavior.SUCCESS"，谁都不认识
    assert resolve_intent("invalid", Behavior.SUCCESS) == "valid"


# ── 3. 判定规则 ─────────────────────────────────────────
def test_resolve_is_idempotent_for_every_known_value():
    """接收「意图」参数的函数会在内部再解析一遍，所以必须幂等。"""
    behaviors = [None, ""] + sorted(BEHAVIOR_TO_INTENT)
    for data_type in sorted(KNOWN_INTENTS) + [None, "", "unknown"]:
        for behavior in behaviors:
            once = resolve_intent(data_type, behavior)
            assert resolve_intent(once, behavior) == once, (data_type, behavior)
            assert resolve_intent(once) == once, (data_type, behavior)


def test_boundary_without_behavior_is_valid_by_default_but_unknown_for_generation():
    assert resolve_intent("boundary") == "valid"
    assert resolve_intent("boundary", assume_ambiguous_valid=False) == "boundary"
    # 有声明时两种模式一致
    for behavior, expected in (("success", "valid"), ("client_error", "invalid")):
        assert resolve_intent("boundary", behavior) == expected
        assert resolve_intent("boundary", behavior, assume_ambiguous_valid=False) == expected


@pytest.mark.parametrize("data_type, expected", [
    ("异常测试", "client_error"),
    ("安全测试", "client_error"),
    ("abnormal", "client_error"),
    ("auth_missing", "client_error"),
    ("功能测试", "success"),
    ("valid", "success"),
    ("边界测试", None),        # 合法边界该成功、越界该被拒，类型分不出来
    ("boundary", None),
    ("manual", None),          # 中性：不表达应成功还是应被拒
    ("兼容性测试", None),
    ("没见过的类型", None),
    (None, None),
])
def test_default_behavior_follows_the_type(data_type, expected):
    """生成器一律写 success 时，「异常测试」「安全测试」被声明成应当成功，
    负向用例期望成功的假通过就再也查不出来。库里有 49 条是这样来的。"""
    assert intent_mod.default_behavior(data_type) == expected


def test_default_behavior_never_contradicts_the_type():
    """补出来的声明解析回去，意图必须和类型本身一致。"""
    for data_type in sorted(KNOWN_INTENTS):
        behavior = intent_mod.default_behavior(data_type)
        if behavior is None:
            continue
        by_type = resolve_intent(data_type, assume_ambiguous_valid=False)
        by_declaration = resolve_intent(data_type, behavior)
        assert (by_type in NEGATIVE_INTENTS) == (by_declaration in NEGATIVE_INTENTS), data_type
        assert (by_type in POSITIVE_INTENTS) == (by_declaration in POSITIVE_INTENTS), data_type


# ── 4. 生成断言必须解析意图 ─────────────────────────────
def _status(asserts):
    return next(a for a in asserts if a["type"] == "status_code")


def test_boundary_declared_success_gets_success_assertions():
    """回归：平台自愈传原始 data_type，1204 条「boundary + 应当成功」的用例
    会被重写成「状态码 < 500」——什么都拦不住。"""
    status = _status(build_assertions(default_convention(), intent="boundary",
                                      expected_behavior="success"))
    assert status["operator"] == "in"
    assert all(200 <= int(x) < 300 for x in status["expected"])


def test_boundary_declared_rejection_gets_rejection_assertions():
    conv = default_convention()
    asserts = build_assertions(conv, intent="boundary", expected_behavior="client_error")
    assert asserts != build_assertions(conv, intent="valid")
    assert asserts == build_assertions(conv, intent="invalid")


def test_boundary_without_declaration_is_not_guessed():
    """生成器造的边界值是 -1 / 0 / MAX_INT，越界的本该被拒。没有声明就不猜。"""
    status = _status(build_assertions(default_convention(), intent="boundary"))
    assert (status["operator"], status["expected"]) == ("lt", 500)


def test_rewrite_passes_expected_behavior_through():
    existing = [{"type": "status_code", "operator": "in", "expected": [200, 200, 201, 204]}]
    rewritten = rewrite_assertions(existing, default_convention(), intent="boundary",
                                   expected_behavior="success")
    assert _status(rewritten)["operator"] == "in"


# ── 5. 执行有效性用同一份意图、同一种快照解析 ────────────
def _row(snapshot, *, data_type="valid", behavior="success", status="passed"):
    return {"status": status, "response_snapshot": snapshot,
            "data_type": data_type, "expected_behavior": behavior}


def test_string_snapshots_are_parsed():
    """落库快照常是 JSON 字符串。当 dict 取，每条都成了「没拿到响应」。"""
    reached = json.dumps({"status_code": 200, "body": {"code": 0, "data": []}})
    result = scan_results([_row(reached)] * 12)
    assert result["by_outcome"][REACHED] == 12
    assert result["verdict"] == OK

    expired = json.dumps({"status_code": 200,
                          "body": {"code": 401, "message": "Access token expired"}})
    result = scan_results([_row(expired)] * 12)
    assert result["by_outcome"][AUTH_REJECTED] == 12
    assert result["passed_but_blocked"] == 12


def test_evidence_uses_the_shared_intent():
    """「正向」以前只有平台认，harness 这边不算正向用例，整轮判不出来。"""
    expired = {"status_code": 200, "body": {"code": 401, "message": "Access token expired"}}
    result = scan_results([_row(expired, data_type="正向", behavior=None)] * 10)
    assert result["positive_cases"] == 10
    assert result["verdict"] == UNUSABLE

    # 负向用例拿到鉴权错误可能完全正确，不参与判定
    result = scan_results([_row(expired, data_type="安全测试", behavior=None)] * 10)
    assert result["positive_cases"] == 0


def test_environment_usable():
    assert scan_results([]) is None
    assert environment_usable(None) is None
    assert environment_usable({}) is None

    ok = {"status_code": 200, "body": {"code": 0}}
    expired = {"status_code": 401, "body": None}
    assert environment_usable(scan_results([_row(ok)] * 5)) is True
    assert environment_usable(scan_results([_row(expired)] * 5)) is False
    # 只有负向用例：没有依据，不下结论
    assert environment_usable(scan_results([_row(expired, data_type="auth_missing",
                                                 behavior="client_error")] * 5)) is None


def test_known_intents_cover_both_polarities():
    assert POSITIVE_INTENTS and NEGATIVE_INTENTS
    assert not (POSITIVE_INTENTS & NEGATIVE_INTENTS)
