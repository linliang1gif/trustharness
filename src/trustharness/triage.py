# -*- coding: utf-8 -*-
"""失败分诊 (Failure Triage)。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# 意图的词表与判定统一在 trustharness.intent。这里以前抄了一份，已经和自检
# 那份漂开：「安全测试」在自检里算负向，在这里不认识——同一条用例，
# 自检说「异常用例断言了成功」，分诊却按「意图未标注」处理它的失败。
from trustharness.intent import (  # noqa: F401  (再导出，保持旧导入可用)
    AMBIGUOUS_INTENTS,
    BEHAVIOR_TO_INTENT,
    NEGATIVE_INTENTS,
    POSITIVE_INTENTS,
    resolve_intent,
)

# ── 分诊类别 ────────────────────────────────────────────────
ENV_UNAVAILABLE = "env_unavailable"      # 环境或接口不可用
INVALID_CASE = "invalid_case"            # 用例生成无效
AUTH_EXPIRED = "auth_expired"            # 鉴权或登录态失效
BAD_ASSERTION = "bad_assertion"          # 断言配置错误
LIKELY_DEFECT = "likely_defect"          # 疑似真实缺陷
NEEDS_REVIEW = "needs_review"            # 证据不足，人工复核

CATEGORY_LABELS = {
    ENV_UNAVAILABLE: "环境或接口不可用",
    INVALID_CASE: "用例生成无效",
    AUTH_EXPIRED: "鉴权或登录态失效",
    BAD_ASSERTION: "断言配置错误",
    LIKELY_DEFECT: "疑似真实缺陷",
    NEEDS_REVIEW: "证据不足，需人工复核",
}

# 谁该看这条失败
CATEGORY_OWNER = {
    ENV_UNAVAILABLE: "环境/运维",
    INVALID_CASE: "用例生成",
    AUTH_EXPIRED: "环境/运维",
    BAD_ASSERTION: "用例生成",
    LIKELY_DEFECT: "开发",
    NEEDS_REVIEW: "测试",
}

# 只有这一类值得提缺陷
ACTIONABLE_AS_DEFECT = {LIKELY_DEFECT}

# 业务码里代表成功的取值
BUSINESS_OK = {0, 200, "0", "200", "success", "SUCCESS", True}
AUTH_CODES = {401, 403, "401", "403"}
# 网关层的「上游不可用」：请求没到业务服务，与被测接口的实现无关
GATEWAY_UNAVAILABLE = {502, 503, 504}
SERVER_ERROR_CODES = {500, 5000, "500", "5000"}

# 只在 error_message 这一个字段上做匹配，绝不匹配拼接文本
_ENV_PATTERNS = re.compile(
    r"连接失败|连接超时|请求超时|timed?\s*out|connection\s*(refused|reset|closed|aborted)"
    r"|ERR_CONNECTION|EOF occurred|_ssl\.c|SSLError|\bSSL\b|\bDNS\b|max retries|unreachable|network is",
    re.IGNORECASE,
)
_CASE_BROKEN_PATTERNS = re.compile(
    r"缺少执行配置|Invalid URL|No scheme supplied|缺少\s*(method|url)|未配置请求",
    re.IGNORECASE,
)
_LOGIN_REDIRECT_PATTERNS = re.compile(
    r"登录态失效|重定向到登录页|redirect(ed)?\s*to\s*login|/login",
    re.IGNORECASE,
)
_RUNNER_PATTERNS = re.compile(
    r"Executable doesn't exist|BrowserType\.launch|playwright.*(未安装|not installed)",
    re.IGNORECASE,
)


def is_transport_failure(error_message: Any) -> bool:
    """error_message 是否说明请求在传输层就失败了、没拿到响应（R1 的判据）。

    执行有效性（evidence）也用它：没有 HTTP 状态码的记录，只有这类失败
    才算「请求没进去」的证据。两处必须同一个判据。
    """
    return bool(error_message) and bool(_ENV_PATTERNS.search(str(error_message)))


def is_login_redirect(error_message: Any) -> bool:
    """error_message 是否说明请求被重定向到了登录页（R3 的判据）。"""
    return bool(error_message) and bool(_LOGIN_REDIRECT_PATTERNS.search(str(error_message)))


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def extract_http_status(response: Any) -> Optional[int]:
    """取 HTTP 状态码。注意与 body.code(业务码) 区分。"""
    resp = _as_dict(response)
    if not resp:
        return None
    for key in ("status_code", "statusCode", "http_status"):
        if key in resp:
            try:
                return int(resp[key])
            except (TypeError, ValueError):
                return None
    # 部分执行器把 HTTP 码写在 status
    value = resp.get("status")
    if isinstance(value, int):
        return value
    return None


def extract_business_code(response: Any) -> Any:
    """取业务码 body.code。本项目多数接口用 HTTP 200 + body.code 表达错误。"""
    resp = _as_dict(response)
    if not resp:
        return None
    body = resp.get("body")
    if not isinstance(body, dict):
        body = resp.get("data") if isinstance(resp.get("data"), dict) else None
    if not isinstance(body, dict):
        # 响应体本身可能就是业务信封，但要避开把 HTTP 码当业务码
        body = {k: v for k, v in resp.items() if k not in ("status_code", "statusCode", "http_status", "status")}
    return body.get("code") if isinstance(body, dict) else None


def extract_business_message(response: Any) -> str:
    """取业务响应里的可读错误信息。"""
    resp = _as_dict(response)
    body = resp.get("body") if isinstance(resp.get("body"), dict) else resp
    if not isinstance(body, dict):
        return ""
    return str(body.get("message") or body.get("msg") or body.get("errMsg") or "").strip()


def _request_has_unresolved_test_data(request: Any, business_message: str) -> bool:
    """判断失败是否来自生成器没有准备接口要求的业务数据。"""
    req = _as_dict(request)
    chain = req.get("data_chain") if isinstance(req.get("data_chain"), dict) else {}
    if chain.get("unresolved"):
        return True
    # 被测系统已经明确指出缺必填值。此时把 Swagger 生成的“正常用例”
    # 报成产品缺陷没有意义，应先补齐请求数据再重跑。
    if re.search(
        r"不能为空|为必填|必填参数|not\s+null|can't\s+be\s+blank|cannot\s+be\s+blank|missing|required",
        business_message,
        re.IGNORECASE,
    ):
        return True
    body = req.get("body")
    return body in (None, {}, "") and bool(re.search(r"不存在|not\s+found", business_message, re.IGNORECASE))


def business_ok(code: Any) -> bool:
    return code is None or code in BUSINESS_OK


def _failed_assertions(assertion_details: Any) -> List[Dict[str, Any]]:
    out = []
    for item in _as_list(assertion_details):
        if isinstance(item, dict) and item.get("passed") is False:
            out.append(item)
    return out


def _assertion_expects_http_error(failed: List[Dict[str, Any]]) -> bool:
    """断言是否期望 HTTP 4xx/5xx —— 用于识别「接口用 200+业务码表达错误」的模板缺陷。"""
    for item in failed:
        if (item.get("type") or item.get("assertion_type")) != "status_code":
            continue
        expected = item.get("expected")
        candidates = expected if isinstance(expected, (list, tuple)) else [expected]
        for value in candidates:
            try:
                if 400 <= int(value) < 600:
                    return True
            except (TypeError, ValueError):
                continue
    return False


# 信封字段：断言这些字段而响应里根本没有，说明是断言写错了，不是接口的错
ENVELOPE_VOCABULARY = frozenset({
    "code", "errCode", "errcode", "resultCode", "status", "success",
    "message", "msg", "errMsg", "data", "result", "payload",
})


def _asserts_absent_envelope_field(failed: List[Dict[str, Any]],
                                   response: Any) -> bool:
    """失败的断言是否都指向「响应里根本不存在的信封字段」。"""
    body = _response_body(response)
    if body is None:
        return False

    targets = set()
    for item in failed:
        kind = str(item.get("type") or item.get("assertion_type") or "")
        if kind not in ("json_path", "field_exists", "field_equals"):
            continue
        path = item.get("path") or item.get("field") or ""
        if isinstance(path, str) and path:
            targets.add(path)

    roots = {path.split(".")[0].split("[")[0] for path in targets}
    if not targets or not roots <= ENVELOPE_VOCABULARY:
        return False
    # 所有目标路径都不在响应体里，才判定为断言写错。不能只看第一段：
    # data=[] 时根字段存在，但 data.list / data.total 仍然是模板臆造的路径。
    def path_exists(value: Any, path: str) -> bool:
        current = value
        for part in path.split("."):
            name = part.split("[")[0]
            if not isinstance(current, dict) or name not in current:
                return False
            current = current[name]
        return True

    return all(not path_exists(body, path) for path in targets)


def _response_body(response: Any) -> Optional[Dict[str, Any]]:
    resp = _as_dict(response)
    if not resp:
        return None
    body = resp.get("body")
    if isinstance(body, dict):
        return body
    data = resp.get("data")
    return data if isinstance(data, dict) else None


def triage(
    *,
    error_message: Optional[str] = None,
    error_type: Optional[str] = None,
    response_snapshot: Any = None,
    request_snapshot: Any = None,
    assertion_details: Any = None,
    data_type: Optional[str] = None,
    status: Optional[str] = None,
    expected_behavior: Optional[str] = None,
    environment_usable: Optional[bool] = None,
) -> Dict[str, Any]:
    """对一条失败执行分诊。

    只依赖结构化字段；error_message 仅用于识别传输层故障，
    且匹配范围限定在该字段内，不与响应体拼接。

    environment_usable 是**整轮**的判定（来自 evidence.scan），
    单条证据推不出来。传 False 表示这一轮的请求普遍没走到业务逻辑，
    此时鉴权类失败一律归环境，不再怪用例——见下面 R4.5。
    None 表示没有整轮信息，行为与加这个参数之前完全一致。

    返回 category / label / owner / confidence / reason / suggestion /
    actionable_as_defect / evidence。
    """
    message = (error_message or "").strip()
    intent = resolve_intent(data_type, expected_behavior)
    # boundary 没声明预期时，上面按「应当成功」解析——多数规则这样判没问题，
    # 但「被接口拒绝」这一条不行：越界值被拒是正确行为，合法边界值被拒才是缺陷
    undeclared_boundary = resolve_intent(
        data_type, expected_behavior, assume_ambiguous_valid=False) in AMBIGUOUS_INTENTS
    http = extract_http_status(response_snapshot)
    biz = extract_business_code(response_snapshot)
    biz_message = extract_business_message(response_snapshot)
    failed = _failed_assertions(assertion_details)
    has_response = bool(_as_dict(response_snapshot))

    evidence = {
        "http_status": http,
        "business_code": biz,
        "business_message": biz_message[:200],
        "data_type": intent or None,
        "failed_assertions": [
            (a.get("type") or a.get("assertion_type")) for a in failed
        ][:5],
        "has_response": has_response,
        "environment_usable": environment_usable,
    }

    def result(category, confidence, reason, suggestion):
        return {
            "category": category,
            "label": CATEGORY_LABELS[category],
            "owner": CATEGORY_OWNER[category],
            "confidence": confidence,
            "reason": reason,
            "suggestion": suggestion,
            "actionable_as_defect": category in ACTIONABLE_AS_DEFECT,
            "evidence": evidence,
        }

    # ── R1 环境/传输层不可用：请求根本没拿到响应 ──
    if _RUNNER_PATTERNS.search(message):
        return result(ENV_UNAVAILABLE, 0.95,
                      "执行器自身不可用（浏览器未安装等），未真正触达被测系统",
                      "在执行机安装依赖（如 playwright install chromium）后重跑")
    if _ENV_PATTERNS.search(message):
        return result(ENV_UNAVAILABLE, 0.93,
                      f"传输层故障，未拿到响应：{message[:60]}",
                      "先确认被测环境可达（域名/证书/网络），再整批重跑")

    # ── R2 用例本身不可执行 ──
    # 必须排在「无响应」兜底之前：这类失败同样没有响应快照，
    # 但成因是用例缺配置，不是环境不可达。
    if _CASE_BROKEN_PATTERNS.search(message):
        return result(INVALID_CASE, 0.95,
                      f"用例缺少可执行配置：{message[:60]}",
                      "补齐 method/url 或删除该用例；检查生成模板是否漏填")

    # ── R3 登录态失效（注意与「预期鉴权失败」区分）──
    # 同样要排在「无响应」兜底之前：被重定向到登录页时往往也没有响应快照。
    if _LOGIN_REDIRECT_PATTERNS.search(message) and intent not in NEGATIVE_INTENTS:
        return result(AUTH_EXPIRED, 0.9,
                      "请求被重定向到登录页，登录态已失效",
                      "刷新登录态/重新获取 token 后重跑；考虑接入登录态保活")

    # ── R4 无响应兜底：以上具体规则都没命中，才归为环境问题 ──
    if not has_response and http is None:
        return result(ENV_UNAVAILABLE, 0.72,
                      "无响应快照，请求可能未发出或被中断",
                      "检查执行器与目标环境连通性；确认用例是否配置了 base_url")

    is_auth_code = http in (401, 403) or biz in AUTH_CODES
    if is_auth_code:
        # ── R4.5 整轮都没打进去时，鉴权失败一律是环境的事 ──
        # 下面那条「负向用例拿到鉴权错误 = 断言写错了」的推理有个前提：
        # 这个鉴权错误是接口**针对这条请求**做出的正确判断。令牌过期时
        # 前提不成立——所有请求都拿同一个错误，和用例写得怎么样无关。
        # 不先拦这一层，一次令牌过期就会把几千条用例判成生成器缺陷，
        if environment_usable is False:
            return result(AUTH_EXPIRED, 0.95,
                          f"整轮请求普遍卡在鉴权层（HTTP {http} / 业务码 {biz}），"
                          "这条失败与用例本身无关",
                          "刷新登录态后整批重跑；在环境恢复之前，"
                          "本轮的分诊结论都不具备参考价值")

        # auth_missing 意图拿到 401 == 接口行为正确，是断言写错了
        if intent in NEGATIVE_INTENTS:
            return result(BAD_ASSERTION, 0.88,
                          f"用例意图是 {intent}（预期被拒），接口确实返回了鉴权错误，"
                          "属于正确行为，却被判为失败",
                          "把断言改为「期望鉴权失败」而非「期望成功」")
        return result(AUTH_EXPIRED, 0.9,
                      f"鉴权失败（HTTP {http} / 业务码 {biz}），而用例意图是正常访问",
                      "检查测试账号权限与 token 有效期；确认环境鉴权配置")

    # ── R5 路径不存在 ──
    if http == 404:
        return result(INVALID_CASE, 0.85,
                      "接口返回 404，目标路径不存在",
                      "核对 Swagger 与实际部署版本；可能是生成用例时用了过期路径")

    # ── R5.5 网关报上游不可用：环境问题，不是被测接口的缺陷 ──
    if http in GATEWAY_UNAVAILABLE:
        return result(ENV_UNAVAILABLE, 0.9,
                      f"网关返回 {http}，上游服务不可用，请求没有到达被测接口",
                      "确认被测环境恢复后重跑这些用例；连续大量出现说明执行期间环境宕过")

    # ── R6 服务端错误 ──
    if http is not None and http >= 500:
        return result(LIKELY_DEFECT, 0.9,
                      f"服务端返回 {http}，无论用例意图如何都不应发生",
                      "按缺陷处理，附请求快照给开发")

    # ── R6.5 业务码表达的服务端异常 ──
    # 必须排在按意图判定之前：异常用例收到「服务器开小差」不是「接口正确拒绝」，
    # 是没做输入校验、异常直接抛出来了
    if biz in SERVER_ERROR_CODES:
        return result(LIKELY_DEFECT, 0.85 if intent in POSITIVE_INTENTS else 0.75,
                      f"接口以业务码 {biz} 返回服务端异常"
                      + ("，正常请求就触发" if intent in POSITIVE_INTENTS else "，异常输入没有被校验拦下"),
                      "按缺陷处理，附请求快照给开发；异常输入触发的应补参数校验")

    # ── R6.8 生成器没有准备必需业务数据 ──
    if (not business_ok(biz) and intent in POSITIVE_INTENTS
            and _request_has_unresolved_test_data(request_snapshot, biz_message)):
        return result(INVALID_CASE, 0.93,
                      f"请求缺少被测接口要求的业务数据：{biz_message[:100] or '存在未解析的数据依赖'}",
                      "从上游查询结果注入真实标识或补齐必填参数后重跑；不得直接转为产品缺陷")

    # ── R7 接口返回业务错误：靠「用例意图」判定归属 ──
    if not business_ok(biz):
        if intent in NEGATIVE_INTENTS:
            if _assertion_expects_http_error(failed):
                return result(BAD_ASSERTION, 0.92,
                              f"接口用 HTTP 200 + 业务码 {biz} 表达错误，"
                              "但断言期望 HTTP 4xx —— 断言模板与接口风格不匹配",
                              "修正断言模板：该接口族用 body.code 表达错误，"
                              "异常用例应断言 business_code 非成功")
            return result(BAD_ASSERTION, 0.85,
                          f"用例意图是 {intent}（预期被拒），接口以业务码 {biz} 正确拒绝，"
                          "却被判为失败",
                          "把断言改为「期望业务码为错误」")
        if undeclared_boundary:
            # 按「应当成功」判会把正确拒绝越界值的接口记成疑似缺陷，
            # 缺陷单是这里代价最大的误判——宁可交给人看
            return result(NEEDS_REVIEW, 0.5,
                          f"边界值用例被业务拒绝（业务码 {biz}），但没有声明预期：越界值被拒"
                          "是正确行为，合法边界值被拒才是缺陷，单看类型分不出",
                          "为该用例补 expected_behavior（success 或 client_error），再重新分诊")
        if intent in POSITIVE_INTENTS:
            return result(LIKELY_DEFECT, 0.78,
                          f"正常用例被业务拒绝（业务码 {biz}），接口未按预期返回成功",
                          "确认请求参数是否齐全；若参数无误则按缺陷处理")
        return result(NEEDS_REVIEW, 0.5,
                      f"接口返回业务码 {biz}，但用例未标注意图（data_type 为空），无法判定预期",
                      "为该用例补 data_type，再重新分诊")

    # ── R8 HTTP 200 且业务成功，仍判失败 ──
    if http == 200 and business_ok(biz):
        if intent in NEGATIVE_INTENTS:
            return result(INVALID_CASE, 0.9,
                          f"用例意图是 {intent}（应被拒绝），但请求成功返回 —— "
                          "异常用例实际发送了合法请求",
                          "检查生成逻辑：非法/边界数据未真正写入请求体或参数")
        schema_failures = [a for a in failed if (a.get("type") or a.get("assertion_type")) == "schema"]
        if schema_failures and intent not in NEGATIVE_INTENTS:
            # 返回结构来自源码声明，真实响应对不上：接口的实际输出偏离了它自己的代码定义
            detail = str(schema_failures[0].get("actual") or schema_failures[0].get("message") or "")[:120]
            return result(LIKELY_DEFECT, 0.8,
                          f"请求成功，但返回结构与源码声明不符：{detail}",
                          "对照源码里的返回类型核对该字段：序列化配置、字段类型或返回对象被改动")
        if failed:
            kinds = ", ".join(str(k) for k in evidence["failed_assertions"] if k) or "未知"
            # 断言指向响应里根本不存在的信封字段时，错在断言不在接口
            if _asserts_absent_envelope_field(failed, response_snapshot):
                return result(BAD_ASSERTION, 0.85,
                              f"断言了响应里不存在的信封字段（{kinds}）——"
                              "接口从不返回该字段，这条断言必然失败",
                              "按接口实际的响应结构生成断言；"
                              "探测响应信封后只断言确认存在的字段")
            return result(LIKELY_DEFECT, 0.6,
                          f"请求成功但断言未通过（{kinds}），响应内容与预期不符",
                          "对比响应字段与预期值：可能是接口返回变更，也可能是断言取值路径写错")
        return result(NEEDS_REVIEW, 0.45,
                      "请求成功且无失败断言明细，失败原因不明",
                      "检查执行器是否正确记录了断言结果")

    return result(NEEDS_REVIEW, 0.4,
                  f"未命中已知规则（HTTP {http} / 业务码 {biz} / 意图 {intent or '未标注'}）",
                  "人工复核后补充分诊规则")
