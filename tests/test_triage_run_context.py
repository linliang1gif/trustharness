# -*- coding: utf-8 -*-
"""整轮判定对分诊的影响。"""
from trustharness.triage import (
    AUTH_EXPIRED,
    BAD_ASSERTION,
    CATEGORY_OWNER,
    triage,
)

# 复现那次事故的响应：HTTP 全绿，错误藏在 body 里
EXPIRED = {"status_code": 200, "body": {"code": 401, "message": "Access token expired: eyJ..."}}


def _triage_negative(**extra):
    return triage(
        response_snapshot=EXPIRED,
        data_type="invalid",
        expected_behavior="client_error",
        status="failed",
        **extra,
    )


def test_without_run_context_negative_case_blames_the_generator():
    """不传整轮判定时，行为与加这个参数之前完全一致。

    这是兼容性断言：老调用方不传参数，结论不能变。
    """
    verdict = _triage_negative()
    assert verdict["category"] == BAD_ASSERTION
    assert CATEGORY_OWNER[BAD_ASSERTION] == "用例生成"


def test_unusable_environment_flips_the_owner_to_ops():
    """整轮没打进去时，同一条失败归环境，不再怪用例。"""
    verdict = _triage_negative(environment_usable=False)
    assert verdict["category"] == AUTH_EXPIRED
    assert CATEGORY_OWNER[AUTH_EXPIRED] == "环境/运维"
    assert "整轮" in verdict["reason"]
    # 置信度要高于逐条推断——整轮证据比单条强
    assert verdict["confidence"] >= 0.95


def test_usable_environment_keeps_the_original_judgement():
    """环境确认可用时，原来的推理依然成立，不能反向误伤。"""
    verdict = _triage_negative(environment_usable=True)
    assert verdict["category"] == BAD_ASSERTION


def test_positive_case_is_auth_expired_either_way():
    """正向用例拿到鉴权错误，本来就归环境；整轮判定不该改变它。"""
    for usable in (None, True, False):
        verdict = triage(
            response_snapshot=EXPIRED,
            data_type="valid",
            expected_behavior="success",
            status="failed",
            environment_usable=usable,
        )
        assert verdict["category"] == AUTH_EXPIRED, usable


def test_non_auth_failures_are_untouched_by_run_context():
    """整轮判定只管鉴权这一类。别的失败不能被它带偏——
    环境坏了不等于所有失败都是环境的错。"""
    server_error = {"status_code": 500, "body": {"message": "boom"}}
    a = triage(response_snapshot=server_error, data_type="valid",
               expected_behavior="success", status="failed")
    b = triage(response_snapshot=server_error, data_type="valid",
               expected_behavior="success", status="failed",
               environment_usable=False)
    assert a["category"] == b["category"]


def test_context_is_recorded_in_evidence():
    """结论要能回溯到依据。看到 auth_expired 时得知道是哪种推理给出的。"""
    verdict = _triage_negative(environment_usable=False)
    assert verdict["evidence"]["environment_usable"] is False
