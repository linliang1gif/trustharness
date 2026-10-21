# -*- coding: utf-8 -*-
"""error_convention 单元测试。"""
import sys
from pathlib import Path

import pytest


from trustharness.conventions import (  # noqa: E402
    BUSINESS_CODE,
    HTTP_STATUS,
    MIXED,
    UNKNOWN,
    build_assertions,
    default_convention,
    infer_from_spec,
    learn_from_history,
    merge,
)


def _spec(responses_per_op, ops=30):
    paths = {}
    for i in range(ops):
        paths[f"/api/x{i}"] = {
            "post": {"responses": {str(c): {"description": ""} for c in responses_per_op}}
        }
    return {"paths": paths}


def _snap(http=200, code=None):
    body = {} if code is None else {"code": code, "message": "x"}
    return {"status_code": http, "body": body}


# ── 从 spec 推断 ────────────────────────────────────────
def test_spec_declaring_only_200_infers_business_code():
    """合成示例：多条路径只声明 200 时应识别业务码错误风格。"""
    conv = infer_from_spec(_spec([200], ops=100))
    assert conv.style == BUSINESS_CODE
    assert conv.asserts_http_errors is False
    assert conv.sample_size == 100


def test_spec_declaring_4xx_infers_http_status():
    conv = infer_from_spec(_spec([200, 400, 404]))
    assert conv.style == HTTP_STATUS
    assert conv.asserts_http_errors is True
    assert 400 in conv.error_http_codes


def test_empty_spec_falls_back_to_default():
    for junk in ({}, {"paths": {}}, None, "not a dict", 123):
        conv = infer_from_spec(junk)
        assert conv.source == "default"
        assert conv.style == MIXED


# ── 从历史学习 ──────────────────────────────────────────
def test_history_dominated_by_business_code():
    snaps = [_snap(200, 10000) for _ in range(80)] + [_snap(200, 200) for _ in range(20)]
    conv = learn_from_history(snaps)
    assert conv.style == BUSINESS_CODE
    assert conv.code_field == "code"


def test_history_dominated_by_http_errors():
    snaps = [_snap(400) for _ in range(80)] + [_snap(200, 200) for _ in range(20)]
    conv = learn_from_history(snaps)
    assert conv.style == HTTP_STATUS


def test_history_mixed():
    """测试可信度相关逻辑。"""
    snaps = ([_snap(200, 10000) for _ in range(65)]
             + [_snap(500) for _ in range(35)]
             + [_snap(200, 200) for _ in range(50)])
    conv = learn_from_history(snaps)
    assert conv.style == MIXED


def test_history_too_small_falls_back():
    conv = learn_from_history([_snap(200, 200) for _ in range(5)])
    assert conv.source == "default"


def test_history_without_errors_is_unknown():
    conv = learn_from_history([_snap(200, 200) for _ in range(50)])
    assert conv.style == UNKNOWN


def test_history_tolerates_garbage():
    conv = learn_from_history([None, "", "not json", 123, [], {}, _snap(200, 200)])
    assert conv is not None


def test_history_confidence_grows_with_samples():
    small = learn_from_history([_snap(200, 10000) for _ in range(20)])
    large = learn_from_history([_snap(200, 10000) for _ in range(500)])
    assert large.confidence > small.confidence


# ── 合并 ────────────────────────────────────────────────
def test_history_outweighs_spec():
    """实际行为比文档可信：spec 说只有 200，但历史观察到大量 HTTP 错误。"""
    spec_conv = infer_from_spec(_spec([200], ops=100))
    hist_conv = learn_from_history([_snap(400) for _ in range(500)])
    merged = merge(spec_conv, hist_conv)
    assert merged.source == "history"
    assert merged.style == HTTP_STATUS


def test_merge_falls_back_when_both_unknown():
    unknown = learn_from_history([_snap(200, 200) for _ in range(50)])
    merged = merge(None, unknown)
    assert merged.source == "default"


# ── 按约定生成断言 ──────────────────────────────────────
def test_business_code_convention_never_asserts_http_4xx():
    """核心回归：接口恒返 200 时，异常用例不得断言 HTTP 4xx。"""
    conv = infer_from_spec(_spec([200], ops=100))
    for intent in ("invalid", "boundary", "required_missing", "type_error"):
        asserts = build_assertions(conv, intent=intent)
        for a in asserts:
            expected = a.get("expected")
            if a["type"] == "status_code_in" and isinstance(expected, list):
                assert not any(400 <= int(x) < 600 for x in expected), (
                    f"意图 {intent} 不该断言 HTTP 错误码，实际 {expected}")


def test_business_code_convention_asserts_business_error():
    """用标准的 json_path + not_in 表达「业务码必须是错误值」，
    而不是发明一个执行引擎不认识的新类型。"""
    conv = infer_from_spec(_spec([200], ops=100))
    asserts = build_assertions(conv, intent="invalid")
    rejection = [a for a in asserts
                 if a["type"] == "json_path" and a.get("operator") == "not_in"]
    assert rejection, f"缺少业务码拒绝断言: {asserts}"
    assert rejection[0]["path"] == "code"


def test_http_status_convention_asserts_4xx():
    conv = infer_from_spec(_spec([200, 400, 422]))
    asserts = build_assertions(conv, intent="invalid")
    status = next(a for a in asserts if a["type"] == "status_code")
    assert any(400 <= int(x) < 600 for x in status["expected"])


def test_positive_intent_asserts_success():
    conv = infer_from_spec(_spec([200], ops=100))
    asserts = build_assertions(conv, intent="valid")
    status = next(a for a in asserts if a["type"] == "status_code")
    assert all(200 <= int(x) < 300 for x in status["expected"])


def test_unlabelled_intent_only_asserts_no_crash():
    """意图未标注时不猜业务预期，只断言服务端没崩。"""
    conv = default_convention()
    asserts = build_assertions(conv, intent="")
    status = next(a for a in asserts if a["type"] == "status_code")
    assert status["operator"] == "lt" and status["expected"] == 500
    # 不猜业务预期
    assert not [a for a in asserts if a["type"] == "json_path"]


@pytest.mark.parametrize("intent", ["invalid", "valid", "boundary", "", "manual"])
def test_generated_assertions_never_have_duplicates(intent):
    """回归锁定：原模板产出过 [200, 200, 201, 204] 这类重复期望值，"""
    for conv in (default_convention(),
                 infer_from_spec(_spec([200], ops=100)),
                 infer_from_spec(_spec([200, 400]))):
        for a in build_assertions(conv, intent=intent):
            expected = a.get("expected")
            if isinstance(expected, list):
                flat = [str(x) for x in expected]
                assert len(flat) == len(set(flat)), f"期望值重复: {expected}"


@pytest.mark.parametrize("intent", ["invalid", "valid", "boundary", "", "manual"])
def test_generated_assertions_are_never_empty(intent):
    asserts = build_assertions(default_convention(), intent=intent)
    assert asserts, "生成的断言不能为空，否则执行结果无法判定"


def test_generated_assertions_pass_self_check():
    """自洽性：按约定生成的断言，必须能通过用例自检。"""
    from trustharness.selfcheck import check_case, ERROR

    for style_spec, ops in ((_spec([200], ops=100), 100), (_spec([200, 400]), 30)):
        conv = infer_from_spec(style_spec)
        for intent in ("invalid", "boundary", "required_missing", "valid"):
            issues = check_case(
                title="接口调用",
                data_type=intent,
                execution_config={"method": "POST", "url": "/a", "body": {"x": ""}},
                assertions=build_assertions(conv, intent=intent),
            )
            blocking = [i for i in issues if i["severity"] == ERROR]
            assert not blocking, (
                f"约定 {conv.style} / 意图 {intent} 生成的断言未通过自检: {blocking}")


# ── 外科式重写：不得误伤结构断言 ────────────────────────
from trustharness.conventions import rewrite_assertions  # noqa: E402


def _layered_assertions():
    """生成器对 L1 主用例的分层断言：基础 + 字段存在 + 业务码 + 结构。"""
    return [
        {"type": "status_code", "expected": 200},
        {"type": "response_time", "expected": 10000},
        {"type": "field_exists", "path": "code"},
        {"type": "field_exists", "path": "message"},
        {"type": "field_equals", "path": "code", "expected": 200},
        {"type": "field_exists", "path": "data.list"},
        {"type": "field_exists", "path": "data.total"},
    ]


def test_rewrite_preserves_structural_assertions():
    """只换错误约定相关的状态断言，L4 结构断言必须保留。

    整体替换会让用例变弱——修一个缺陷却引入另一个。
    """
    conv = infer_from_spec(_spec([200], ops=100))
    out = rewrite_assertions(_layered_assertions(), conv, intent="valid")
    paths = {a.get("path") for a in out if a["type"].startswith("field_")}
    assert {"code", "message", "data.list", "data.total"} <= paths
    assert any(a["type"] == "field_equals" for a in out)


def test_rewrite_replaces_status_assertions():
    conv = infer_from_spec(_spec([200], ops=100))
    out = rewrite_assertions(
        [{"type": "status_code_in", "expected": [400, 422, 500]}],
        conv, intent="invalid")
    status = [a for a in out if a["type"] == "status_code"]
    assert len(status) == 1
    assert not any(400 <= int(x) < 600 for x in status[0]["expected"])
    # 旧类型名不得残留
    assert not [a for a in out if a["type"] == "status_code_in"]


def test_rewrite_does_not_duplicate_response_time():
    conv = infer_from_spec(_spec([200], ops=100))
    out = rewrite_assertions(_layered_assertions(), conv, intent="valid")
    assert sum(1 for a in out if a["type"] == "response_time") == 1


def test_rewrite_tolerates_empty_and_garbage():
    conv = infer_from_spec(_spec([200], ops=100))
    for junk in (None, [], [None, "x", 123], [{"no_type": 1}]):
        out = rewrite_assertions(junk, conv, intent="valid")
        assert out and all(isinstance(a, dict) for a in out)


def test_rewritten_assertions_pass_self_check():
    from trustharness.selfcheck import check_case, ERROR

    for spec_shape in (_spec([200], ops=100), _spec([200, 400, 422])):
        conv = infer_from_spec(spec_shape)
        for intent in ("valid", "invalid", "boundary", "auth_missing", "required_missing"):
            out = rewrite_assertions(_layered_assertions(), conv, intent=intent)
            issues = check_case(
                title="接口调用",
                data_type=intent,
                execution_config={"method": "POST", "url": "/a", "body": {"x": ""}},
                assertions=out,
            )
            blocking = [i for i in issues if i["severity"] == ERROR]
            assert not blocking, f"{conv.style}/{intent}: {blocking}"


# 注：端到端「平台生成器产出能通过自检」的测试留在平台侧
# （ai-test-platform/tests/regression/test_error_convention.py），
# 它测的是平台生成器与本包的集成，不属于本包自身的职责。


# ── 生成器 × 执行引擎 的契约 ────────────────────────────
def test_generated_assertion_types_are_all_executable():
    """build_assertions 产出的每个断言类型，执行引擎都必须支持。

    回归锁定一个真实事故：早先版本发明了 status_code_in /
    business_code_not_in / error_signalled 这类新类型，执行引擎
    不认识、一律判失败——生成的用例在自检里 100% 通过，
    真实执行时 100% 失败。没有任何测试守着生成器与引擎之间的契约。
    """
    pytest.importorskip("httpx")
    from trustharness.runner.models import AssertionType

    supported = {t.value for t in AssertionType}
    produced = set()
    conventions = [
        default_convention(),
        infer_from_spec(_spec([200], ops=100)),
        infer_from_spec(_spec([200, 400, 422])),
    ]
    for conv in conventions:
        for intent in ("valid", "invalid", "boundary", "auth_missing",
                       "required_missing", "manual", ""):
            for a in build_assertions(conv, intent=intent):
                produced.add(a["type"])

    unsupported = produced - supported
    assert not unsupported, (
        f"这些断言类型执行引擎不支持，生成出来必然失败: {sorted(unsupported)}。"
        f"引擎支持的是 {sorted(supported)}。")


def test_generated_operators_are_all_executable():
    """断言里用到的 operator，执行引擎的 _compare 必须能处理。"""
    pytest.importorskip("httpx")
    from trustharness.runner.assertion_engine import AssertionEngineV2

    known = {"eq", "ne", "gt", "lt", "gte", "lte", "in", "not_in", "contains", "regex"}
    produced = set()
    for conv in (default_convention(), infer_from_spec(_spec([200], ops=100)),
                 infer_from_spec(_spec([200, 400]))):
        for intent in ("valid", "invalid", "boundary", ""):
            for a in build_assertions(conv, intent=intent):
                if a.get("operator"):
                    produced.add(a["operator"])

    assert produced <= known, f"未知 operator: {sorted(produced - known)}"
    # 且 _compare 实际能算出结果
    for op in produced:
        result = AssertionEngineV2._compare(200, [200, 201] if op in ("in", "not_in") else 500, op)
        assert isinstance(result, bool)


# ── 响应信封探测 ────────────────────────────────────────
from trustharness.conventions import (  # noqa: E402
    detect_envelope_from_history,
    detect_envelope_from_spec,
)


def _spec_with_schema(props, ops=20):
    """构造带 200 响应 schema 的 spec。"""
    paths = {}
    for i in range(ops):
        paths[f"/api/x{i}"] = {"get": {"responses": {"200": {
            "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {k: {"type": "string"} for k in props},
            }}}}}}}
    return {"paths": paths}


def _snap_body(body, http=200):
    return {"status_code": http, "body": body}


def test_envelope_detected_from_spec():
    envelope = detect_envelope_from_spec(_spec_with_schema(["code", "message", "data"]))
    assert envelope == ["code", "data", "message"]


def test_envelope_absent_when_spec_has_no_business_fields():
    """响应是裸业务对象时，信封为空列表——「已确认没有信封」。"""
    envelope = detect_envelope_from_spec(_spec_with_schema(["id", "name", "created_at"]))
    assert envelope == []


def test_envelope_unknown_when_schema_coverage_is_thin():
    """覆盖率不足时返回 None（不知道），而不是凭薄证据下结论。

    平台自身的 spec 只有 34/356 个操作带 schema，据此判定
    「没有 message 字段」会误删有效断言——实际响应里是有的。
    """
    spec = _spec_with_schema(["code", "message"], ops=3)
    for i in range(50):                       # 大量无 schema 的操作
        spec["paths"][f"/api/bare{i}"] = {"get": {"responses": {"200": {}}}}
    assert detect_envelope_from_spec(spec) is None


def test_envelope_detected_from_history():
    snaps = [_snap_body({"code": 200, "message": "ok", "data": {}}) for _ in range(20)]
    assert detect_envelope_from_history(snaps) == ["code", "data", "message"]


def test_envelope_empty_from_history_when_no_business_envelope():
    """真实场景：平台自身各端点响应形状不一，没有任何信封字段
    稳定出现（超过半数）——判定为「已确认没有信封」。

    注意分母只算 dict 响应：返回裸数组的端点不参与统计。
    """
    snaps = ([_snap_body({"message": "x", "version": "1"}) for _ in range(4)]
             + [_snap_body({"id": 1, "name": "a"}) for _ in range(8)]
             + [_snap_body({"total": 3, "items": []}) for _ in range(8)])
    assert detect_envelope_from_history(snaps) == []


def test_envelope_field_at_majority_is_counted():
    """超过半数 dict 响应都带的字段，算作信封字段。"""
    snaps = ([_snap_body({"message": "x"}) for _ in range(12)]
             + [_snap_body({"id": 1}) for _ in range(6)])
    assert detect_envelope_from_history(snaps) == ["message"]


def test_non_dict_bodies_are_excluded_from_envelope_denominator():
    """返回裸数组的端点不参与信封统计，否则会把分母冲淡。"""
    snaps = ([_snap_body({"code": 0}) for _ in range(10)]
             + [_snap_body([1, 2, 3]) for _ in range(40)])
    assert detect_envelope_from_history(snaps) == ["code"]


def test_envelope_unknown_from_history_when_too_few_samples():
    assert detect_envelope_from_history([_snap_body({"code": 200})] * 5) is None


# ── 信封决定断言 ────────────────────────────────────────
def test_no_business_code_assertion_when_field_confirmed_absent():
    """已确认响应没有 code 字段时，不得再断言它——否则必然失败。"""
    conv = infer_from_spec(_spec_with_schema(["id", "name"]))
    conv.style = BUSINESS_CODE
    for intent in ("valid", "invalid", "boundary"):
        for a in build_assertions(conv, intent=intent):
            assert not (a["type"] == "json_path" and a.get("path") == "code"), \
                f"意图 {intent} 不该断言不存在的 code 字段"


def test_business_code_assertion_kept_when_field_present():
    conv = infer_from_spec(_spec_with_schema(["code", "message", "data"]))
    conv.style = BUSINESS_CODE
    kinds = [(a["type"], a.get("path")) for a in build_assertions(conv, intent="invalid")]
    assert ("json_path", "code") in kinds


def test_rewrite_drops_absent_envelope_assertions():
    """回归锁定：生成器无条件加 field_exists:code，"""
    conv = infer_from_spec(_spec_with_schema(["id", "name"]))
    generated = [
        {"type": "status_code", "expected": 200},
        {"type": "field_exists", "path": "code"},
        {"type": "field_equals", "path": "code", "expected": 200},
        {"type": "field_exists", "path": "data.list"},
        {"type": "field_exists", "path": "items"},      # 业务字段，应保留
    ]
    out = rewrite_assertions(generated, conv, intent="valid")
    paths = {a.get("path") for a in out}
    assert "code" not in paths
    assert "data.list" not in paths
    assert "items" in paths, "业务字段断言不该被误删"


def test_rewrite_keeps_envelope_assertions_when_evidence_is_missing():
    """证据不足（envelope_fields 为 None）时一律保留，不悄悄削弱测试。"""
    conv = default_convention()
    assert conv.envelope_fields is None
    generated = [{"type": "field_exists", "path": "code"}]
    out = rewrite_assertions(generated, conv, intent="valid")
    assert any(a.get("path") == "code" for a in out)


# ── merge 合并的是两个正交事实 ──────────────────────────
def test_merge_takes_envelope_from_history_even_when_spec_style_wins():
    """回归锁定：早先 merge 整体二选一，spec 的 style 赢了之后
    历史观测到的信封字段被一并丢弃，field_exists:code 继续照发。"""
    spec_conv = infer_from_spec(_spec([200, 400, 422], ops=40))   # 高置信度
    spec_conv.envelope_fields = None
    hist_conv = learn_from_history(
        [_snap_body({"id": 1, "name": "x"}) for _ in range(20)])

    merged = merge(spec_conv, hist_conv)
    assert merged.style == spec_conv.style, "风格应取置信度高的 spec"
    assert merged.envelope_fields == [], "信封应取自直接观测的历史"


def test_merge_prefers_history_envelope_over_spec_envelope():
    spec_conv = infer_from_spec(_spec_with_schema(["code", "message"], ops=40))
    hist_conv = learn_from_history(
        [_snap_body({"id": 1}) for _ in range(20)])
    assert merge(spec_conv, hist_conv).envelope_fields == []


# ── 运算符契约 ──────────────────────────────────────────
def test_engine_supports_all_common_operators():
    """引擎必须支持常见断言运算符。"""
    pytest.importorskip("httpx")
    from trustharness.runner.assertion_engine import AssertionEngineV2

    required = {"eq", "ne", "gt", "lt", "gte", "lte",
                "in", "not_in", "contains", "regex",
                "exists", "not_empty", "type"}
    for op in required:
        result = AssertionEngineV2._compare(200, [200] if op in ("in", "not_in") else 200, op)
        assert isinstance(result, bool), f"运算符 {op} 未被支持"


@pytest.mark.parametrize("value,type_name,expected", [
    (200, "integer", True),
    (200, "string", False),
    ("200", "string", True),
    (1.5, "number", True),
    (True, "boolean", True),
    (True, "integer", False),          # bool 是 int 的子类，不能算 integer
    ([], "array", True),
    ({}, "object", True),
    (None, "null", True),
    (200, "无此类型", False),
])
def test_type_assertion_semantics(value, type_name, expected):
    pytest.importorskip("httpx")
    from trustharness.runner.assertion_engine import AssertionEngineV2

    assert AssertionEngineV2._match_type(value, type_name) is expected


def test_legacy_assertion_type_aliases_are_executable():
    """历史用例里的 status_code_in / json_field 等别名必须能执行。"""
    pytest.importorskip("httpx")
    from trustharness.runner.assertion_engine import AssertionEngineV2
    from trustharness.runner.models import AssertionDef, HttpResponse

    engine = AssertionEngineV2()
    response = HttpResponse(status_code=200, body={"code": 200, "message": "ok"})

    for assertion, should_pass in [
        (AssertionDef(type="status_code_in", expected=[200, 201]), True),
        (AssertionDef(type="status_code_in", expected=[400]), False),
        (AssertionDef(type="json_field", path="code", operator="eq", expected=200), True),
        (AssertionDef(type="json_path_equals", path="code", operator="eq", expected=200), True),
    ]:
        result = engine._run_single(assertion, response)
        assert result.passed is should_pass, f"{assertion.type} 判定错误: {result.message}"


def test_ui_assertions_are_skipped_not_failed():
    """Web UI 断言由 playwright 引擎执行。API 引擎判它失败，
    会让 Web UI 用例走 API 通道时产生假失败。"""
    pytest.importorskip("httpx")
    from trustharness.runner.assertion_engine import AssertionEngineV2
    from trustharness.runner.models import AssertionDef, HttpResponse

    engine = AssertionEngineV2()
    response = HttpResponse(status_code=200, body={})
    for ui_type in ("element_visible", "text_visible", "url_contains"):
        result = engine._run_single(
            AssertionDef(type=ui_type, path=".btn", expected=True), response)
        assert result.passed is True
        assert "跳过" in result.message


def test_assertion_result_records_operator():
    """不记录 operator 就无法复现判定，失败记录也就无法被审计。"""
    pytest.importorskip("httpx")
    from trustharness.runner.assertion_engine import AssertionEngineV2
    from trustharness.runner.models import AssertionDef, HttpResponse

    engine = AssertionEngineV2()
    response = HttpResponse(status_code=200, body={"code": 200})

    for assertion in [
        AssertionDef(type="json_path", path="code", operator="type", expected="integer"),
        AssertionDef(type="json_path", path="code", operator="exists", expected=True),
        AssertionDef(type="status_code", operator="in", expected=[200, 201]),
        AssertionDef(type="response_time", expected=10000),
    ]:
        payload = engine._run_single(assertion, response).to_dict()
        assert "operator" in payload, f"{assertion.type} 的结果没记录 operator"
        assert payload["operator"], "operator 不能为空"


def test_assertion_result_is_replayable():
    """从 to_dict() 的记录必须能重建出同样的判定——这是可审计的底线。"""
    pytest.importorskip("httpx")
    from trustharness.runner.assertion_engine import AssertionEngineV2
    from trustharness.runner.models import AssertionDef, HttpResponse

    engine = AssertionEngineV2()
    response = HttpResponse(status_code=200, body={"code": 200, "message": "ok"})

    for original in [
        AssertionDef(type="json_path", path="code", operator="type", expected="integer"),
        AssertionDef(type="json_path", path="code", operator="type", expected="string"),
        AssertionDef(type="json_path", path="nosuch", operator="exists", expected=True),
        AssertionDef(type="status_code", operator="in", expected=[200]),
    ]:
        recorded = engine._run_single(original, response).to_dict()
        rebuilt = AssertionDef(
            type=recorded["type"], path=recorded["path"],
            operator=recorded["operator"], expected=recorded["expected"])
        replayed = engine._run_single(rebuilt, response)
        assert replayed.passed == recorded["passed"], (
            f"重放结果不一致: {recorded} → {replayed.passed}")
