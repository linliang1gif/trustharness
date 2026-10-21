# -*- coding: utf-8 -*-
"""执行证据的回归测试。"""
from trustharness.evidence import (
    AUTH_REJECTED,
    DEGRADED,
    GATEWAY_ERROR,
    NO_RESPONSE,
    OK,
    REACHED,
    UNUSABLE,
    classify,
    scan,
)
from trustharness.score import compute


# ── 单条分类 ────────────────────────────────────────────
def test_http_401_is_auth_rejected():
    assert classify(401, None) == AUTH_REJECTED
    assert classify(403, {"data": 1}) == AUTH_REJECTED


def test_business_code_401_under_http_200_is_auth_rejected():
    """最要命的一种：HTTP 全绿，错误藏在 body 里。"""
    body = {"code": 401, "message": "Access token expired: eyJhbGciOi..."}
    assert classify(200, body) == AUTH_REJECTED


def test_auth_message_without_known_code():
    assert classify(200, {"code": 99999, "message": "登录已失效，请重新登录"}) == AUTH_REJECTED
    assert classify(200, {"code": 99999, "msg": "token invalid"}) == AUTH_REJECTED


def test_business_permission_error_is_not_environment_problem():
    """「无权限操作该订单」是被测系统的正确行为，不是环境坏了。

    把它判成鉴权失效，会让一批正常的权限校验用例把整次执行判死。
    """
    assert classify(200, {"code": 5003, "message": "无权限操作该订单"}) == REACHED


def test_gateway_and_missing_response():
    assert classify(502, None) == GATEWAY_ERROR
    assert classify(None, None) == NO_RESPONSE
    # 传输层超时用 0 占位，正文是异常描述而不是业务响应
    assert classify(0, "bounded HTTP failure: ReadTimeout") == NO_RESPONSE


def test_json_string_body_is_parsed():
    assert classify(200, '{"code": 401, "message": "token expired"}') == AUTH_REJECTED


def test_plain_success_reaches():
    assert classify(200, {"code": 0, "data": {"id": 7}}) == REACHED
    assert classify(200, "not json at all") == REACHED
    assert classify(404, {"message": "not found"}) == REACHED   # 业务上的找不到，进去了


# ── 整轮扫描 ────────────────────────────────────────────
EXPIRED = {"code": 401, "message": "Access token expired: eyJ..."}


def test_token_expired_run_is_unusable():
    """复现那次事故：正向用例全部没进去。"""
    records = (
        [{"positive_intent": True, "passed": True, "status_code": 200, "body": EXPIRED}] * 60
        + [{"positive_intent": False, "passed": False, "status_code": 200, "body": EXPIRED}] * 40
    )
    result = scan(records)
    assert result["verdict"] == UNUSABLE
    assert result["validity"] == 0.0
    # 判了通过但根本没进去的，必须单独数出来
    assert result["passed_but_blocked"] == 60
    assert "不算数" in result["headline"]


def test_healthy_run_is_ok():
    records = [{"positive_intent": True, "passed": True, "status_code": 200,
                "body": {"code": 0, "data": {}}}] * 50
    result = scan(records)
    assert result["verdict"] == OK
    assert result["validity"] == 1.0
    assert result["passed_but_blocked"] == 0


def test_partial_auth_failure_is_degraded():
    records = (
        [{"positive_intent": True, "passed": True, "status_code": 200,
          "body": {"code": 0}}] * 80
        + [{"positive_intent": True, "passed": False, "status_code": 401, "body": None}] * 20
    )
    result = scan(records)
    assert result["verdict"] == DEGRADED
    assert result["validity"] == 0.8


def test_negative_cases_alone_do_not_condemn_a_run():
    """只有反向用例拿到鉴权错误时不下结论——那可能完全正确。"""
    records = [{"positive_intent": False, "passed": True, "status_code": 401,
                "body": None}] * 30
    result = scan(records)
    assert result["verdict"] == OK
    assert result["validity"] is None


# ── 与评分的联动 ────────────────────────────────────────
def _perfect_selfcheck(n=100):
    return {f"TC_{i}": [] for i in range(n)}


def test_execution_validity_caps_the_score():
    """用例写得再好，请求没打进去，分数也不能高。"""
    evidence = scan([{"positive_intent": True, "passed": True,
                      "status_code": 200, "body": EXPIRED}] * 100)

    without = compute(selfcheck_results=_perfect_selfcheck())
    with_evidence = compute(selfcheck_results=_perfect_selfcheck(),
                            execution_evidence=evidence)

    assert without.overall == 100.0
    assert with_evidence.overall == 0.0
    assert with_evidence.raw_overall == 100.0      # 被谁压的要看得见
    assert with_evidence.execution["verdict"] == UNUSABLE
    assert any("不算数" in c for c in with_evidence.caveats)


def test_healthy_evidence_does_not_lower_the_score():
    evidence = scan([{"positive_intent": True, "passed": True, "status_code": 200,
                      "body": {"code": 0}}] * 100)
    scored = compute(selfcheck_results=_perfect_selfcheck(), execution_evidence=evidence)
    assert scored.overall == 100.0


def test_score_without_evidence_is_unchanged():
    """不传证据时行为必须和以前完全一样，否则是破坏性改动。"""
    scored = compute(selfcheck_results=_perfect_selfcheck())
    assert scored.execution is None
    assert scored.overall == scored.raw_overall


# ── 没有 HTTP 响应的记录：只有明确指向「没进去」的才算证据 ──
# 「0 条请求卡在鉴权层」。那 32 次背后的 446 条记录，406 条是 skipped
# （「functional 用例需手动执行」），13 条卡在 running——一条请求都没发过；
# 其余是 Web UI 步骤失败，本来就没有 HTTP 状态码。它们全被当成了
# 「正向请求没拿到响应」。
from trustharness.evidence import scan_results  # noqa: E402


def _result(status, *, snapshot=None, message=None, data_type="功能测试"):
    return {"status": status, "response_snapshot": snapshot, "error_message": message,
            "data_type": data_type, "expected_behavior": "success"}


def test_never_executed_cases_are_not_evidence():
    rows = [_result("skipped", message="functional 用例需手动执行")] * 116
    result = scan_results(rows)
    assert result["without_evidence"] == 116
    assert result["positive_cases"] == 0
    assert result["validity"] is None
    assert result["verdict"] == OK


def test_web_ui_step_failures_are_not_evidence():
    rows = ([_result("failed", message="Step 3 (click) 失败: 元素 .btn-delete 未找到")] * 7
            + [_result("passed", snapshot={"console_error_count": 0, "screenshots": []})] * 5)
    result = scan_results(rows)
    assert result["without_evidence"] == 12
    assert result["verdict"] == OK


def test_transport_failures_still_condemn_the_run():
    """环境整个连不上，照样要判不算数——而且要说清是没拿到响应，不是鉴权。"""
    rows = [_result("error", message="连接失败: Connection refused")] * 20
    result = scan_results(rows)
    assert result["by_outcome"][NO_RESPONSE] == 20
    assert result["verdict"] == UNUSABLE
    assert "没拿到响应" in result["headline"]
    assert "鉴权" not in result["headline"]


def test_login_redirect_counts_as_auth_rejected():
    """Web UI 的「令牌过期」：被重定向到登录页。"""
    rows = [_result("failed", message="Step 1 (goto) 失败: [登录态失效] 目标 /list 被重定向到登录页")] * 8
    result = scan_results(rows)
    assert result["by_outcome"][AUTH_REJECTED] == 8
    assert result["verdict"] == UNUSABLE


def test_unusable_headline_counts_the_dominant_reason():
    """「0 条请求卡在鉴权层」这种话不能再出现。"""
    rows = ([_result("error", message="请求超时 timed out")] * 30
            + [_result("passed", snapshot={"status_code": 200, "body": EXPIRED})] * 5)
    result = scan_results(rows)
    assert result["verdict"] == UNUSABLE
    assert "30 条请求没拿到响应" in result["headline"]


def test_auth_missing_cases_getting_401_are_not_fake_passes():
    """「没带令牌应当被拒」的用例拿到 401，是测到了它要测的东西。

    以前照样算进「判了通过但请求没进业务逻辑」：健康的执行里也冒出这条警告；
    令牌过期那一轮的 2825 条里有 416 条是这种。
    """
    ok = {"status_code": 200, "body": {"code": 0}}
    rejected = {"status_code": 200, "body": {"code": 401, "message": "未登录"}}
    healthy = ([_result("passed", snapshot=ok, data_type="valid")] * 20
               + [{**_result("passed", snapshot=rejected, data_type="auth_missing"),
                   "expected_behavior": "client_error"}] * 4)
    result = scan_results(healthy)
    assert result["passed_but_blocked"] == 0
    assert result["verdict"] == OK

    # 正向用例拿着 401 判了通过，照样要数出来
    fake = [_result("passed", snapshot={"status_code": 200, "body": EXPIRED}, data_type="valid")] * 3
    assert scan_results(healthy + fake)["passed_but_blocked"] == 3


def test_records_without_outcome_keep_old_semantics():
    """直接调 scan() 的旧调用方：没给 outcome 时，没有状态码仍按「没拿到响应」算。"""
    result = scan([{"positive_intent": True, "passed": False,
                    "status_code": None, "body": None}] * 10)
    assert result["by_outcome"][NO_RESPONSE] == 10
    assert result["verdict"] == UNUSABLE
    assert result["without_evidence"] == 0
