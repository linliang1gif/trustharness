# -*- coding: utf-8 -*-
"""case_self_check 单元测试。"""
import sys
from pathlib import Path

import pytest


from trustharness.selfcheck import (  # noqa: E402
    ERROR,
    WARNING,
    check_case,
    summarize,
)


def codes(issues):
    return {i["code"] for i in issues}


def severities(issues):
    return {i["severity"] for i in issues}


# ── 可执行性 ────────────────────────────────────────────
def test_missing_method_is_error():
    """测试可信度相关逻辑。"""
    issues = check_case(execution_config={"url": "/a"})
    assert "missing_execution_config" in codes(issues)
    assert ERROR in severities(issues)


def test_missing_url_is_error():
    issues = check_case(execution_config={"method": "GET"})
    assert "missing_execution_config" in codes(issues)


def test_unexecutable_case_short_circuits():
    """不可执行时不再报后续问题——否则一条废用例会刷出一堆噪声。"""
    issues = check_case(execution_config={}, assertions=[], data_type="invalid")
    assert codes(issues) == {"missing_execution_config"}


def test_relative_url_without_scheme_is_error():
    issues = check_case(
        execution_config={"method": "GET", "url": "api/users"},
        assertions=[{"type": "status_code", "expected": 200}],
    )
    assert "invalid_url" in codes(issues)


def test_root_relative_url_is_accepted():
    issues = check_case(
        execution_config={"method": "GET", "url": "/api/users"},
        assertions=[{"type": "status_code", "expected": 200}],
        data_type="valid",
    )
    assert "invalid_url" not in codes(issues)


def test_no_assertion_is_error():
    issues = check_case(execution_config={"method": "GET", "url": "/a"}, assertions=[])
    assert "no_assertion" in codes(issues)


# ── 核心：意图与断言是否自洽 ────────────────────────────
def test_negative_intent_expecting_success_is_error():
    """异常用例断言期望 2xx 是假通过：接口正常返回于是判「通过」，
    实际一个异常分支都没验证到。比失败更危险。

    注意用 data_type=invalid + expected_behavior=client_error——
    这是无歧义的负向意图。boundary 不算（见下面的语义测试）。
    """
    issues = check_case(
        title="获取出库单list - 异常测试",
        data_type="invalid",
        expected_behavior="client_error",
        execution_config={"method": "POST", "url": "/common/home/listItems",
                          "body": {"dateType": ""}},
        assertions=[{"type": "status_code", "operator": "in", "expected": [200, 201, 204]}],
    )
    assert "negative_intent_expects_success" in codes(issues)
    assert ERROR in severities(issues)


# ── 意图判定：data_type 描述数据，expected_behavior 描述预期 ──
def test_boundary_with_success_behavior_is_positive_intent():
    """合法范围内的边界值应当成功——断言期望 2xx 是对的，不是缺陷。"""
    issues = check_case(
        title="分页查询 - 边界测试(最小页长)",
        data_type="boundary",
        expected_behavior="success",
        execution_config={"method": "POST", "url": "/a", "body": {"pageSize": 1}},
        assertions=[{"type": "status_code", "operator": "in", "expected": [200, 201, 204]}],
    )
    assert "negative_intent_expects_success" not in codes(issues)


def test_boundary_with_client_error_behavior_is_negative_intent():
    """刚越界的值应当被拒——此时断言期望 2xx 才是错的。"""
    issues = check_case(
        title="分页查询 - 边界测试(越界页长)",
        data_type="boundary",
        expected_behavior="client_error",
        execution_config={"method": "POST", "url": "/a", "body": {"pageSize": 999999}},
        assertions=[{"type": "status_code", "operator": "in", "expected": [200, 201]}],
    )
    assert "negative_intent_expects_success" in codes(issues)


def test_ambiguous_intent_without_behavior_defaults_to_positive():
    """没有 expected_behavior 时，边界值按「应当成功」处理。"""
    from trustharness.selfcheck import resolve_intent

    assert resolve_intent("boundary", None) == "valid"
    assert resolve_intent("boundary", "client_error") == "invalid"
    assert resolve_intent("invalid", None) == "invalid"
    assert resolve_intent("invalid", "client_error") == "invalid"
    assert resolve_intent("valid", "success") == "valid"


def test_expected_behavior_overrides_data_type():
    """expected_behavior 是显式声明，比 data_type 权威。"""
    from trustharness.selfcheck import resolve_intent

    # 数据形态是 boundary，但明确声明应当成功
    assert resolve_intent("boundary", "success") == "valid"
    # 数据形态未标注，但明确声明应当被拒
    assert resolve_intent(None, "client_error") == "invalid"


def test_negative_intent_expecting_4xx_is_clean():
    issues = check_case(
        data_type="invalid",
        execution_config={"method": "POST", "url": "/a", "body": {"x": ""}},
        assertions=[{"type": "status_code", "operator": "in", "expected": [400, 422]}],
    )
    assert "negative_intent_expects_success" not in codes(issues)


def test_positive_intent_expecting_error_is_warning():
    issues = check_case(
        data_type="valid",
        execution_config={"method": "GET", "url": "/a"},
        assertions=[{"type": "status_code", "expected": [400]}],
    )
    assert "positive_intent_expects_error" in codes(issues)
    assert ERROR not in severities(issues)


def test_auth_missing_expecting_401_is_clean():
    """auth_missing 用例期望 401 是正确的。"""
    issues = check_case(
        data_type="auth_missing",
        execution_config={"method": "POST", "url": "/a", "body": {}, "headers": {}},
        assertions=[{"type": "status_code_in", "expected": [401, 403]}],
    )
    assert "negative_intent_expects_success" not in codes(issues)


# ── 断言自身一致性 ──────────────────────────────────────
def test_duplicate_expected_is_flagged():
    """测试可信度相关逻辑。"""
    issues = check_case(
        data_type="valid",
        execution_config={"method": "GET", "url": "/a"},
        assertions=[{"type": "status_code", "expected": [200, 200, 201, 204]}],
    )
    assert "duplicate_expected" in codes(issues)


def test_unique_expected_is_clean():
    issues = check_case(
        data_type="valid",
        execution_config={"method": "GET", "url": "/a"},
        assertions=[{"type": "status_code", "expected": [200, 201, 204]}],
    )
    assert "duplicate_expected" not in codes(issues)


# ── 标题与 payload 是否对得上 ───────────────────────────
def test_title_claims_missing_field_but_body_is_full():
    """标题「异常测试(缺少必填参数)」，
    body 却是 SQL 注入串——跑出任何结果都无法解读。"""
    issues = check_case(
        title="获取记录列表 - 异常测试(缺少必填参数)",
        data_type="invalid",
        execution_config={"method": "POST", "url": "/common/home/listItems",
                          "body": {"dateType": "'; DROP TABLE users; --"}},
        assertions=[{"type": "status_code", "operator": "in", "expected": [400, 422, 404, 403]}],
    )
    assert "title_payload_mismatch" in codes(issues)


def test_title_claims_missing_field_and_body_is_empty_is_clean():
    issues = check_case(
        title="创建订单 - 缺少必填参数",
        data_type="invalid",
        execution_config={"method": "POST", "url": "/a", "body": {}},
        assertions=[{"type": "status_code", "expected": [400]}],
    )
    assert "title_payload_mismatch" not in codes(issues)


def test_title_claims_empty_value_but_fields_have_values():
    issues = check_case(
        title="查询 - 空值测试",
        data_type="boundary",
        execution_config={"method": "POST", "url": "/a", "body": {"name": "abc"}},
        assertions=[{"type": "status_code", "expected": [400]}],
    )
    assert "title_payload_mismatch" in codes(issues)


# ── 异常意图的 payload 是否真的异常 ─────────────────────
def test_negative_intent_without_anomaly_marker():
    """分诊里「异常用例实际发送了合法请求」的生成侧成因。"""
    issues = check_case(
        data_type="invalid",
        execution_config={"method": "POST", "url": "/a", "body": {"name": "normaltext"}},
        assertions=[{"type": "status_code", "expected": [400]}],
    )
    assert "negative_intent_without_anomaly" in codes(issues)


@pytest.mark.parametrize("payload", [
    {"name": ""},
    {"name": None},
    {"id": -1},
    {"q": "'; DROP TABLE users; --"},
    {"q": "<script>alert(1)</script>"},
])
def test_payload_with_anomaly_marker_is_clean(payload):
    issues = check_case(
        data_type="invalid",
        execution_config={"method": "POST", "url": "/a", "body": payload},
        assertions=[{"type": "status_code", "expected": [400]}],
    )
    assert "negative_intent_without_anomaly" not in codes(issues)


# ── 输入容错 ────────────────────────────────────────────
def test_json_string_inputs_are_parsed():
    import json
    issues = check_case(
        data_type="valid",
        execution_config=json.dumps({"method": "GET", "url": "/a"}),
        assertions=json.dumps([{"type": "status_code", "expected": 200}]),
    )
    assert "missing_execution_config" not in codes(issues)


@pytest.mark.parametrize("junk", [None, "", "not json", 123, [], {}])
def test_garbage_config_never_crashes(junk):
    issues = check_case(execution_config=junk, assertions=junk)
    assert isinstance(issues, list)


def test_clean_case_returns_no_issues():
    issues = check_case(
        title="查询币别列表 - 正常流程",
        data_type="valid",
        execution_config={"method": "POST", "url": "/basic/currencies/page",
                          "body": {"pageNum": 1, "pageSize": 10}},
        assertions=[{"type": "status_code", "expected": [200, 201]}],
    )
    assert issues == []


# ── 汇总 ────────────────────────────────────────────────
def test_summarize_counts_blocked_cases():
    data = {
        "a": [],
        "b": [{"code": "duplicate_expected", "severity": WARNING, "message": "", "suggestion": "", "field": None}],
        "c": [{"code": "missing_execution_config", "severity": ERROR, "message": "", "suggestion": "", "field": None}],
    }
    s = summarize(data)
    assert s["total_cases"] == 3
    assert s["clean_cases"] == 1
    assert s["blocked_cases"] == 1          # 只有 error 级才算拦截
    assert s["cases_with_issues"] == 2
    assert 0 <= s["pass_rate"] <= 1


def test_summarize_handles_empty():
    s = summarize({})
    assert s["total_cases"] == 0
    assert s["pass_rate"] == 0


# ── 数字类异常特征只看整串数字 ─────────────────────────────
@pytest.mark.parametrize("value", ["2026-09-16T22:34:35", "13000000000", "ORDER-001"])
def test_ordinary_values_are_not_anomalies(value):
    """原来 "-1" 按子串匹配，日期 2026-09-16 被当成异常数据，
    源码推导规范生成的「缺少必填参数」用例因此误报 title_payload_mismatch。"""
    from trustharness.selfcheck import _has_anomaly

    assert not _has_anomaly({"field": value}, None)


@pytest.mark.parametrize("value", ["-1", "999999", "99999999", " -5 "])
def test_fabricated_numeric_extremes_are_still_anomalies(value):
    from trustharness.selfcheck import _has_anomaly

    assert _has_anomaly({"field": value}, None)
