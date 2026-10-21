# -*- coding: utf-8 -*-
"""独立 harness 的评测主干：assess()，以及公开批量 API 的意图与环境上下文。"""
from trustharness import inspect_cases, triage_failures
from trustharness.evidence import UNUSABLE
from trustharness.pipeline import SMALL_SAMPLE, PipelineResult, assess
from trustharness.triage import AUTH_EXPIRED, BAD_ASSERTION

EXPIRED = {"status_code": 200, "body": {"code": 401, "message": "Access token expired: eyJ..."}}
HEALTHY = {"status_code": 200, "body": {"code": 0, "data": {"id": 1}}}


def _row(case_id, status, snapshot, data_type, expected_behavior=None, assertions=None):
    return {
        "case_id": case_id,
        "status": status,
        "response_snapshot": snapshot,
        "assertion_details": assertions or [],
        "data_type": data_type,
        "expected_behavior": expected_behavior,
        "error_message": None,
    }


def _token_expired_run(n_positive=40, n_negative=40):
    """复现那次事故：正向用例全「通过」（鉴权错误也是 200），反向用例全失败。"""
    rows = []
    for i in range(n_positive):
        rows.append(_row(f"P{i}", "passed", EXPIRED, "valid", "success"))
    for i in range(n_negative):
        rows.append(_row(
            f"N{i}", "failed", EXPIRED, "invalid", "client_error",
            assertions=[{"type": "status_code", "passed": False,
                         "expected": [400, 422], "actual": 200}]))
    selfcheck = {r["case_id"]: [] for r in rows}
    return selfcheck, rows


# ── 令牌过期的一轮必须被判死 ─────────────────────────────
def test_token_expired_run_is_capped_not_graded():
    selfcheck, rows = _token_expired_run()
    out = assess(selfcheck=selfcheck, rows=rows)

    assert out["execution"]["verdict"] == UNUSABLE
    score = out["score"]
    # 用例本身写得无可挑剔，没有执行证据时是满分——封顶之后必须接近 0
    assert score.raw_overall is not None and score.raw_overall >= 60
    assert score.overall <= out["execution"]["validity_percent"] + 0.05
    assert any("不算数" in n for n in out["notes"]), "第一句提醒必须说破"


def test_token_expired_failures_blame_environment_not_generator():
    """40 条反向用例失败。不传整轮上下文时它们会全部记成「断言配置错误」。"""
    selfcheck, rows = _token_expired_run()
    out = assess(selfcheck=selfcheck, rows=rows)

    categories = {v["category"] for v in out["verdicts"]}
    assert BAD_ASSERTION not in categories, "环境坏了还在怪生成器"
    assert categories == {AUTH_EXPIRED}


def test_healthy_run_is_not_penalised():
    rows = [_row(f"P{i}", "passed", HEALTHY, "valid", "success") for i in range(40)]
    out = assess(selfcheck={r["case_id"]: [] for r in rows}, rows=rows)
    assert out["execution"]["verdict"] != UNUSABLE
    assert out["score"].overall == out["score"].raw_overall
    # 好消息不能出现在「需要注意」里
    assert not any("到达了被测系统" in c for c in out["score"].caveats)


def test_clean_run_is_dynamic_not_static():
    """执行了、一条都没失败，是动态证据，不是「没提供执行结果」。"""
    rows = [_row(f"P{i}", "passed", HEALTHY, "valid", "success") for i in range(40)]
    score = assess(selfcheck={r["case_id"]: [] for r in rows}, rows=rows)["score"]
    assert score.evidence_level == "dynamic"
    assert "静态" not in score.grade
    assert not any("未提供执行结果" in c for c in score.caveats)
    purity = next(d for d in score.dimensions if d.key == "signal_purity")
    # 没有失败就没有可衡量的信号：不打分，而不是给个满分
    assert purity.score is None


# ── 样本太小要说出来 ──────────────────────────────────────
def test_small_sample_is_called_out():
    """8 条执行、5 条失败就给「A 级」，是这个包自己要反对的那种过度自信。

    不改分数（分数要可复算），只把样本量小说出来。
    """
    rows = [_row(f"P{i}", "passed", HEALTHY, "valid", "success") for i in range(8)]
    out = assess(selfcheck={r["case_id"]: [] for r in rows}, rows=rows)
    assert any("样本量" in n for n in out["notes"])

    big = [_row(f"P{i}", "passed", HEALTHY, "valid", "success")
           for i in range(SMALL_SAMPLE)]
    out_big = assess(selfcheck={r["case_id"]: [] for r in big}, rows=big)
    assert not any("样本量" in n for n in out_big["notes"])


# ── 公开 API：意图要看 expected_behavior ─────────────────
def test_triage_failures_honours_expected_behavior():
    """expected_behavior 是「应当成功还是应当被拒」的权威声明，必须压过 data_type。

    样本故意让两者冲突：data_type=valid、expected_behavior=client_error。
    第一版用的是 boundary，而 boundary 没有 expected_behavior 时本来就回落成
    正向——新旧代码结论一样，那条测试是空的，修复被删掉它也照样绿。
    """
    row = _row("C1", "failed", {"status_code": 401, "body": None},
               "valid", "client_error")
    verdict = triage_failures([row])[0]
    # 按 behavior 是负向用例：拿到 401 是接口正确拒绝 → 断言写错了
    # 只看 data_type 会当成正向：拿到 401 → 登录态失效
    assert verdict["category"] == BAD_ASSERTION, (
        f"expected_behavior 没被采纳，按 data_type 判成了 {verdict['category']}")


def test_triage_failures_accepts_environment_context():
    row = _row("N1", "failed", EXPIRED, "invalid", "client_error")
    assert triage_failures([row])[0]["category"] == BAD_ASSERTION
    assert triage_failures([row], environment_usable=False)[0]["category"] == AUTH_EXPIRED


def test_inspect_cases_passes_expected_behavior():
    """同一条用例，带不带 expected_behavior，自检结论必须不同。

    样本：data_type=valid 但 expected_behavior=client_error，断言却期望 200。
    按 behavior 它是负向用例断言成功 → 应报 negative_intent_expects_success；
    只看 data_type 它是正向用例断言 200 → 什么都不报。
    """
    base = {
        "id": "C1",
        "title": "缺少必填参数应被拒绝",
        "data_type": "valid",
        "execution_config": {"method": "GET", "url": "/items", "params": {}},
        "assertions": [{"type": "status_code", "operator": "in", "expected": [200]}],
    }
    with_behavior = inspect_cases([{**base, "expected_behavior": "client_error"}])["C1"]
    without = inspect_cases([base])["C1"]

    codes_with = {i["code"] for i in with_behavior}
    codes_without = {i["code"] for i in without}
    assert codes_with != codes_without, (
        "带不带 expected_behavior 结论一样——它没被传进自检")
    assert "negative_intent_expects_success" in codes_with


# ── JSON 输出要带证据 ─────────────────────────────────────
def test_json_output_carries_per_failure_evidence():
    """以前 --json 只给分数：说「5 条疑似真实缺陷」却不说是哪 5 条。"""
    selfcheck, rows = _token_expired_run(n_positive=2, n_negative=3)
    out = assess(selfcheck=selfcheck, rows=rows)

    result = PipelineResult()
    result.verdicts = out["verdicts"]
    result.execution = out["execution"]
    result.score = out["score"]
    payload = result.to_dict()

    assert payload["execution"]["verdict"] == UNUSABLE
    assert len(payload["failures"]) == 3
    for failure in payload["failures"]:
        assert failure["case_id"] and failure["category"] and failure["reason"]
