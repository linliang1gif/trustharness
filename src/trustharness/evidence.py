# -*- coding: utf-8 -*-
"""执行证据 —— 请求到底有没有走到业务逻辑里。"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

# ── 分类结果 ──
REACHED = "reached"              # 进到业务逻辑了，结果可信
AUTH_REJECTED = "auth_rejected"  # 卡在鉴权，没进去
GATEWAY_ERROR = "gateway_error"  # 卡在网关/路由，没进去
NO_RESPONSE = "no_response"      # 没拿到响应

# HTTP 层的鉴权拒绝
AUTH_HTTP = frozenset({401, 403})
# 网关层：请求没到应用
GATEWAY_HTTP = frozenset({502, 503, 504})

# 业务码表达的鉴权错误。用 HTTP 200 + 业务码表错误的接口很常见，
# 这时 HTTP 状态码完全无害，错误藏在 body 里。
AUTH_BUSINESS_CODES = frozenset({401, 403, 1401, 10401, 40100, 40300})

# 兜底：业务码没命中时看消息。只匹配明确指向鉴权的说法，
# 宁可漏判也不要把业务上的「无权限操作该订单」误判成环境问题——
# 那是被测系统的正确行为，不是环境坏了。
AUTH_MESSAGE = re.compile(
    r"access[ _-]?token|token\s*(expired|invalid|missing)|unauthorized|"
    r"未登录|登录已?(失效|过期)|令牌(失效|过期|无效)|鉴权失败|认证失败",
    re.IGNORECASE,
)


def _as_dict(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        import json
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _business_code(body: Dict[str, Any]) -> Optional[int]:
    for key in ("code", "errcode", "errCode", "status", "resultCode"):
        raw = body.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def classify(status_code: Optional[int], body: Any) -> str:
    """单条响应：请求走到业务逻辑了吗。

    只看响应，不看用例意图——意图用在 scan() 那一层决定谁参与判定。
    """
    # 0 不是 HTTP 状态码：传输层超时/断连时用它占位（scenario_http_transport 的 bounded HTTP failure）。
    # 以前只认 None，写请求超时会被当成「已到达业务逻辑」，副作用未知被误判为普通失败。
    if not status_code:
        return NO_RESPONSE
    if status_code in GATEWAY_HTTP:
        return GATEWAY_ERROR
    if status_code in AUTH_HTTP:
        return AUTH_REJECTED

    parsed = _as_dict(body)
    if parsed is None:
        return REACHED

    code = _business_code(parsed)
    if code is not None and code in AUTH_BUSINESS_CODES:
        return AUTH_REJECTED

    message = parsed.get("message") or parsed.get("msg") or ""
    if isinstance(message, str) and AUTH_MESSAGE.search(message):
        return AUTH_REJECTED

    return REACHED


# 正向用例里有多少比例没进去，就算这次执行不可用。
# 放在 0.5：一半以上的正向用例都被挡在门外，剩下那一半的结论也没法单独相信。
UNUSABLE_THRESHOLD = 0.5
# 低于这个就值得提醒，但还不至于把整次执行判死
DEGRADED_THRESHOLD = 0.9

OK = "ok"
DEGRADED = "degraded"
UNUSABLE = "unusable"


def scan(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """整次执行的证据体检。

    records 每条形如::

        {"positive_intent": bool, "passed": bool,
         "status_code": int | None, "body": dict | str | None,
         "outcome": str | None,                      # 可选
         "expects_auth_rejection": bool}             # 可选：用例本来就要被鉴权拒绝

    正向用例（positive_intent=True）是判定依据：它不该拿到鉴权错误。
    反向用例拿到鉴权错误可能完全正确，不参与。

    outcome 给了就不再按状态码分类。给 None 表示「这条记录说明不了请求
    有没有进去」——没执行过的（skipped / running）、没有 HTTP 层的（Web UI、
    手工）都是这种：不进任何判定，只数进 without_evidence。
    不给 outcome 时沿用旧口径：没有状态码就是没拿到响应。
    """
    rows: List[Dict[str, Any]] = list(records)
    total = len(rows)

    buckets: Dict[str, int] = {REACHED: 0, AUTH_REJECTED: 0,
                              GATEWAY_ERROR: 0, NO_RESPONSE: 0}
    positive = 0
    positive_reached = 0
    passed_but_blocked = 0
    without_evidence = 0
    sample: Optional[str] = None

    for row in rows:
        if "outcome" in row:
            verdict = row["outcome"]
            if verdict is None:
                without_evidence += 1
                continue
        else:
            verdict = classify(row.get("status_code"), row.get("body"))
        buckets[verdict] += 1

        if row.get("positive_intent"):
            positive += 1
            if verdict == REACHED:
                positive_reached += 1

        # 判了通过、但请求根本没进去——这是最危险的一类：
        # 报告上是绿的，实际什么都没验证。
        # 例外：用例本来就要验证「没带令牌会被拒」，拿到鉴权错误正是它要的结果。
        # 以前不排除，健康的执行里也会冒出「N 条被判通过的用例请求没走到业务逻辑」
        expected_rejection = verdict == AUTH_REJECTED and row.get("expects_auth_rejection")
        if row.get("passed") and verdict != REACHED and not expected_rejection:
            passed_but_blocked += 1
            if sample is None:
                parsed = _as_dict(row.get("body"))
                if parsed:
                    message = str(parsed.get("message") or parsed.get("msg") or "")
                    sample = f"业务码 {_business_code(parsed)} · {message[:60]}"

    validity = round(positive_reached / positive, 4) if positive else None

    if validity is None:
        verdict = OK               # 没有正向用例可判，不下结论
    elif validity < UNUSABLE_THRESHOLD:
        verdict = UNUSABLE
    elif validity < DEGRADED_THRESHOLD:
        verdict = DEGRADED
    else:
        verdict = OK

    return {
        "total": total,
        "by_outcome": buckets,
        "without_evidence": without_evidence,
        "positive_cases": positive,
        "positive_reached": positive_reached,
        "validity": validity,
        "validity_percent": round(validity * 100, 1) if validity is not None else None,
        "passed_but_blocked": passed_but_blocked,
        "verdict": verdict,
        "sample_signal": sample,
        "headline": _headline(verdict, validity, passed_but_blocked, buckets,
                              without_evidence),
    }


def records_from_results(results: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """执行结果 → scan() 的输入。

    results 与 triage_failures 同一形态：status / response_snapshot /
    data_type / expected_behavior，通过的和失败的都要给——判「这一轮打没打
    进去」恰恰要看通过的那些。

    意图用 trustharness.intent 判，状态码用分诊同一个取法。平台以前自己
    实现了这一步，意图词表和 harness 不一样；两边对「这次执行算不算数」
    可能给出不同结论。
    """
    from trustharness.intent import AUTH_REJECTION_INTENTS, POSITIVE_INTENTS, resolve_intent
    from trustharness.triage import extract_http_status

    records: List[Dict[str, Any]] = []
    for row in results or []:
        if not isinstance(row, dict):
            continue
        # 落库的快照可能是 JSON 字符串；当 dict 取会把每条都判成 no_response
        snapshot = _as_dict(row.get("response_snapshot") or row.get("response")) or {}
        status_code = extract_http_status(snapshot)
        intent = resolve_intent(row.get("data_type") or row.get("intent"),
                                row.get("expected_behavior"))
        records.append({
            "positive_intent": intent in POSITIVE_INTENTS,
            "expects_auth_rejection": intent in AUTH_REJECTION_INTENTS,
            "passed": str(row.get("status") or "").lower() == "passed",
            "status_code": status_code,
            "body": snapshot.get("body"),
            "outcome": (classify(status_code, snapshot.get("body")) if status_code is not None
                        else _outcome_without_status(row.get("error_message"))),
        })
    return records


def _outcome_without_status(error_message: Any) -> Optional[str]:
    """没有 HTTP 状态码的记录，只有失败信息明确指向「请求没进去」才算证据。"""
    from trustharness.triage import is_login_redirect, is_transport_failure

    if is_transport_failure(error_message):
        return NO_RESPONSE
    if is_login_redirect(error_message):
        return AUTH_REJECTED
    return None


def scan_results(results: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """执行结果 → 整轮证据体检。

    没有任何结果时返回 None：那是「无从判断」，不是「一切正常」。
    """
    records = records_from_results(results)
    return scan(records) if records else None


def environment_usable(execution: Optional[Dict[str, Any]]) -> Optional[bool]:
    """这一轮的请求有没有走到业务逻辑——分诊需要的整轮上下文。

    None 表示判不出来（没有正向用例可依据），分诊此时维持逐条判断。
    """
    if not execution or execution.get("validity") is None:
        return None
    return execution["verdict"] != UNUSABLE


BLOCKED_REASON = {
    AUTH_REJECTED: "卡在鉴权层",
    GATEWAY_ERROR: "卡在网关层",
    NO_RESPONSE: "没拿到响应（连接失败或超时）",
}


def _headline(verdict: str, validity: Optional[float],
              passed_but_blocked: int, buckets: Dict[str, int],
              without_evidence: int = 0) -> str:
    if verdict == UNUSABLE:
        # 报占比最大的那个原因、报它自己的条数。以前把鉴权和网关加在一起、
        # 原因只在这两者里挑，没拿到响应的既不计数也不点名——
        # 于是出现过「0 条请求卡在鉴权层」
        reason = max(BLOCKED_REASON, key=lambda key: buckets[key])
        extra = (f"，其中 {passed_but_blocked} 条还被判成了通过"
                 if passed_but_blocked else "")
        return (f"这次执行不算数：{buckets[reason]} 条请求{BLOCKED_REASON[reason]}，"
                f"没走到业务逻辑{extra}。先修环境再看结论。")
    if verdict == DEGRADED:
        return (f"只有 {round((validity or 0) * 100, 1)}% 的正向用例真正到达了被测系统，"
                "其余卡在鉴权、网关或没拿到响应——这部分结论不可用。")
    if passed_but_blocked:
        return f"{passed_but_blocked} 条判了通过但请求没进业务逻辑，它们没有验证任何东西。"
    if validity is None:
        # 以前这里也说「请求都到达了被测系统」——一条请求都没发过的执行也这么说
        detail = (f"（{without_evidence} 条记录没执行过或没有 HTTP 响应）"
                  if without_evidence else "")
        return f"没有可依据的正向请求，不评估请求是否到达被测系统{detail}。"
    return "请求都到达了被测系统。"
