# -*- coding: utf-8 -*-
"""用例自检 (Case Self-Check)。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

ERROR = "error"
WARNING = "warning"

# ── 用例意图 ────────────────────────────────────────────────
# 词表与判定统一在 trustharness.intent，这里只是同名导出，
# `from trustharness.selfcheck import NEGATIVE_INTENTS` 这类旧导入照常可用。
#
# 词表必须覆盖所有会写入 data_type / expected_behavior 的取值，否则自检会
# 静默漏判：不认识的意图既不算正向也不算负向，「意图 × 断言」这条核心规则
# 直接跳过。平台的 tests/regression/test_intent_vocabulary.py 会扫描平台源码
# 里写入的字面量和库里实际出现的取值，有未登记的就失败。
from trustharness.intent import (  # noqa: F401  (再导出)
    AMBIGUOUS_INTENTS,
    BEHAVIOR_TO_INTENT,
    KNOWN_INTENTS,
    NEGATIVE_INTENTS,
    NEUTRAL_INTENTS,
    POSITIVE_INTENTS,
    resolve_intent,
)

# payload 里能体现「这是异常数据」的特征
ANOMALY_MARKERS = [
    '""', "''", "null", "None",
    "DROP", "SELECT ", "OR 1=1", "<script", "../", "%00",
    "-1", "999999", "99999999",
    # 类型不符：数字/布尔/数组字段收到的占位串（生成器的「无效参数类型」、L2 的「类型错误」都这么造）
    "not_a_number", "not_a_boolean", "not_an_array", "not_an_object",
]
# 数字类特征只对「整串就是个数字」的取值成立。原来按子串匹配，
# 日期 "2026-09-16" 里含 "-1"、订单号里含 "999999" 都被当成异常数据。
_NUMERIC_MARKERS = {"-1", "999999", "99999999"}
_NUMERIC_STRING = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")

# 不可见的控制字符（不含制表、换行、回车）：这类是典型的非法输入，
# 但不是可读词，靠 ANOMALY_MARKERS 的子串匹配认不出来
_CONTROL_CHARS = re.compile("[%s-%s%s-%s%s]" % (chr(0), chr(8), chr(11), chr(31), chr(127)))

# 超长串的判定长度。原来是 255，而生成器造的溢出数据是 200 字符，一条都没认出来。
# 100 以上的取值在正常业务请求里很少见，用它做界。
_LONG_VALUE = 100

# 「缺少必填参数」「必填缺失: x」「参数缺失」都是同一件事。
# L2 变异生成器写的是「必填缺失: 字段名」，原正则不认，于是它生成的用例一出生就带警告。
_MISSING_FIELD_TITLE = re.compile(r"缺少必填|必填参数|必填缺失|参数缺失|missing\s+required", re.I)
_EMPTY_TITLE = re.compile(r"空值|为空|empty", re.I)


def _as_obj(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


def _status_expectations(assertions: List[Any]) -> List[int]:
    """收集所有 status_code 类断言的期望值（打平成整数列表）。"""
    out: List[int] = []
    for a in assertions:
        if not isinstance(a, dict):
            continue
        kind = str(a.get("type") or a.get("assertion_type") or "")
        if not kind.startswith("status_code"):
            continue
        # operator 为 lt/lte/gt/gte 时，expected 是阈值而非期望取值集合，
        # 把它当成「期望的状态码」会得出错误结论（如 lt 500 被读成期望 500）。
        if str(a.get("operator") or "") in ("lt", "lte", "gt", "gte", "ne", "not_in"):
            continue
        expected = a.get("expected")
        candidates = expected if isinstance(expected, (list, tuple)) else [expected]
        for v in candidates:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
    return out


def _iter_values(node):
    """深度遍历 body / params 里的叶子值。"""
    if isinstance(node, dict):
        for v in node.values():
            yield from _iter_values(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _iter_values(v)
    else:
        yield node


def _has_anomaly(body, params) -> bool:
    """判断请求数据里是否真的带了异常特征。

    早先版本把 [body, params] 整体 json.dumps 后做子串匹配，
    而 params 缺省时会序列化出字面量 null——它恰好也在特征词表里，
    导致几乎所有用例都被误判为「带了异常数据」，这条检查形同虚设
    （全库只标出 12 条）。现改为逐个叶子值判断。
    """
    values = []
    for container in (body, params):
        # 容器整体缺省 ≠ 容器内出现空值，前者不算「带了异常数据」
        if container is None:
            continue
        values.extend(_iter_values(container))

    for value in values:
        if value is None or value == "" or value == [] or value == {}:
            return True                       # 字段被显式置空，属于边界数据
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if value < 0 or abs(value) >= 999999:
                return True
            continue
        if isinstance(value, str):
            upper = value.upper()
            if any(m.upper() in upper for m in ANOMALY_MARKERS if m not in _NUMERIC_MARKERS):
                return True
            # 数字串只认负数和 999999 这类造出来的极值；手机号、单号本身就是长数字，不算异常
            if _NUMERIC_STRING.match(value) and (value.strip().startswith("-") or "999999" in value):
                return True
            if _CONTROL_CHARS.search(value):   # 不可见的非法输入
                return True
            if len(value) >= _LONG_VALUE:      # 超长串属于边界数据
                return True
    return False


def _anomaly_blob(body, params) -> str:
    payload = {}
    if body not in (None, {}):
        payload["body"] = body
    if params not in (None, {}):
        payload["params"] = params
    return json.dumps(payload, ensure_ascii=False) if payload else "(空请求)"


def _asserts_business_rejection(assertions: List[Any]) -> bool:
    """是否存在「业务码必须是错误值」这类拒绝断言。"""
    rejection_types = {
        "business_code_not_in", "business_code_not_equals", "business_code_ne",
        "error_signalled", "code_not_in", "code_not_equals",
    }
    rejection_operators = {"not_equals", "ne", "not_in", "!=", "not-in"}
    for a in assertions:
        if not isinstance(a, dict):
            continue
        kind = str(a.get("type") or a.get("assertion_type") or "")
        if kind in rejection_types:
            return True
        # 标准形式：json_path 断言业务码字段不等于成功值
        if kind in ("json_path", "business_code", "field_equals"):
            if str(a.get("operator") or "") in rejection_operators:
                return True
    return False


# 路径里没被替换掉的参数占位，以及被替换成字面量的空值。
# 两者都让请求打在一个不存在的地址上，必然 404——跑多少次都不会有别的结果。
_UNSUBSTITUTED_PARAM = re.compile(r"\{[^}/]+\}")
_LITERAL_NONE_SEGMENT = re.compile(r"/(None|null|nil|undefined|NaN)(/|$)")


# 不看取值的操作符：只证明字段在、类型对，任何业务码都能通过
_VALUE_BLIND_OPERATORS = {"exists", "type", "not_empty"}

CODE_FIELDS = {"code", "data.code", "$.code", "body.code", "errCode", "errcode"}


def _is_code_assertion(a: Any) -> bool:
    if not isinstance(a, dict):
        return False
    kind = str(a.get("type") or a.get("assertion_type") or "")
    if "business_code" in kind or kind in ("error_signalled", "code_not_in", "code_not_equals"):
        return True
    if kind in ("json_path", "field_equals", "json_path_equals", "field_exists", "json_path_exists"):
        path = str(a.get("path") or a.get("field") or "").lstrip("$.").strip()
        return path in CODE_FIELDS or path.endswith(".code") or path == "code"
    return False


def _asserts_business_code(assertions: List[Any]) -> bool:
    """是否**判了业务码的取值**（不论期望成功还是失败）。"""
    for a in assertions:
        if not _is_code_assertion(a):
            continue
        kind = str(a.get("type") or a.get("assertion_type") or "")
        operator = str(a.get("operator") or ("exists" if "exists" in kind else "eq"))
        if operator not in _VALUE_BLIND_OPERATORS:
            return True
    return False


def _only_transport_assertions(assertions: List[Any]) -> bool:
    """是否只断言了传输层的东西：HTTP 状态码、响应耗时。"""
    kinds = {str(a.get("type") or a.get("assertion_type") or "")
             for a in assertions if isinstance(a, dict)}
    return bool(kinds) and kinds <= {"status_code", "status_code_in", "status_code_not_in",
                                     "response_time"}


def _issue(code, severity, message, suggestion, field=None):
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "suggestion": suggestion,
        "field": field,
    }


def check_case(
    *,
    case_id: Optional[str] = None,
    title: Optional[str] = None,
    data_type: Optional[str] = None,
    execution_config: Any = None,
    assertions: Any = None,
    expected_behavior: Optional[str] = None,
    business_code_style: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """检查单条用例，返回问题列表（空列表表示通过）。

    纯函数，不碰数据库，便于在生成流程里直接调用。

    business_code_style 说明被测接口是不是「HTTP 200 + body.code」风格。
    None 表示不知道（默认），此时不做与该风格有关的判断——不能凭空假设
    别的项目也这样。平台从学到的 error_conventions 里取这个值传进来。
    """
    issues: List[Dict[str, Any]] = []
    cfg = _as_obj(execution_config)
    cfg = cfg if isinstance(cfg, dict) else {}
    asserts = _as_obj(assertions)
    asserts = asserts if isinstance(asserts, list) else []
    intent = resolve_intent(data_type, expected_behavior)
    name = title or ""

    # ── 1. 可执行性 ──
    method = cfg.get("method")
    url = cfg.get("url") or cfg.get("path")
    if not method or not url:
        issues.append(_issue(
            "missing_execution_config", ERROR,
            "缺少 method 或 url，用例无法执行",
            "补齐 execution_config.method / url，或删除该用例",
            "execution_config"))
        return issues  # 不可执行，后续检查没有意义

    url = str(url)
    if not url.startswith("/") and not url.startswith(("http://", "https://")):
        issues.append(_issue(
            "invalid_url", ERROR,
            f"URL 既不是绝对地址也不是根相对路径：{url}",
            "改为以 / 开头的相对路径，或补全 scheme 与域名",
            "execution_config.url"))

    # ── 1.5 路径参数没填好：地址不存在，跑多少次都是 404 ──
    #     生成侧的两个实际缺陷：占位符原样留在 URL 里（/anything/{anything}），
    #     以及取值取到空后被格式化成字面量（/absolute-redirect/None）。
    #     库里 120 条这样的用例，每次执行都稳定失败，还占着失败名额。
    holder = _UNSUBSTITUTED_PARAM.search(url)
    if holder:
        issues.append(_issue(
            "unsubstituted_path_param", ERROR,
            f"URL 里的路径参数没有被替换：{holder.group(0)}（完整路径 {url}）",
            "生成时把路径参数替换成真实取值；取不到值就别生成这条用例",
            "execution_config.url"))
    literal = _LITERAL_NONE_SEGMENT.search(url)
    if literal:
        issues.append(_issue(
            "literal_none_in_path", ERROR,
            f"URL 里有一段是 {literal.group(1)}，说明路径参数取到空值后被直接拼了进去：{url}",
            "取不到路径参数的取值时不要生成用例，或改用有效的样例值",
            "execution_config.url"))

    # ── 2. 断言存在性 ──
    if not asserts:
        issues.append(_issue(
            "no_assertion", ERROR,
            "没有任何断言，执行结果无法判定通过或失败",
            "至少补一条 status_code 或业务码断言",
            "assertions"))
        return issues

    # ── 3. 断言自身的一致性 ──
    for a in asserts:
        if not isinstance(a, dict):
            continue
        expected = a.get("expected")
        if isinstance(expected, list):
            flat = [str(x) for x in expected]
            if len(flat) != len(set(flat)):
                dup = sorted({x for x in flat if flat.count(x) > 1})
                issues.append(_issue(
                    "duplicate_expected", WARNING,
                    f"断言期望值列表有重复项：{expected}（重复 {dup}）",
                    "生成模板里去重；重复值说明期望集合是拼接出来的、未做归并",
                    "assertions"))
                break

    # ── 4. 核心：用例意图 与 断言期望 是否自洽 ──
    status_expected = _status_expectations(asserts)
    asserts_rejection = _asserts_business_rejection(asserts)
    if status_expected:
        all_2xx = all(200 <= s < 300 for s in status_expected)
        all_err = all(400 <= s < 600 for s in status_expected)

        # 注意：接口用「HTTP 200 + 业务错误码」表达错误时，异常用例断言
        # status_code=200 是**正确**的，拒绝体现在业务码断言上。
        # 只有当既期望 2xx、又没有任何业务码拒绝断言时，才是真的期望成功。
        if intent in NEGATIVE_INTENTS and all_2xx and not asserts_rejection:
            issues.append(_issue(
                "negative_intent_expects_success", ERROR,
                f"用例意图是 {intent}（应被拒绝），断言却期望成功状态码 {status_expected}",
                "异常用例应断言接口拒绝：期望 4xx，或在 HTTP 200 + 业务码风格下"
                "断言 business_code 为错误值",
                "assertions"))

        if intent in POSITIVE_INTENTS and all_err:
            issues.append(_issue(
                "positive_intent_expects_error", WARNING,
                f"用例意图是 {intent}（应成功），断言却期望错误状态码 {status_expected}",
                "确认这是不是被错标成了正常用例",
                "assertions"))

    # ── 4.5 业务码风格下「只断言 HTTP 状态码」等于没断言 ──
    #     接口用 HTTP 200 + body.code 表达错误时，无权限、参数错误、系统异常
    #     都是 HTTP 200，只看状态码的用例照样判「通过」。
    # 判了字段在不在、类型对不对，但不判取值 —— 等同于没断言业务码
    value_blind_code = (any(_is_code_assertion(a) for a in asserts)
                        and not _asserts_business_code(asserts))
    if business_code_style and value_blind_code:
        issues.append(_issue(
            "value_blind_code_assertion", WARNING,
            "只断言了业务码字段存在或类型，没有判它的取值——"
            "接口返回参数错误、系统异常时照样判通过",
            "把 exists/type 换成对取值的断言：正向断言 code 等于成功值，异常断言不等于成功值",
            "assertions"))

    file_response = str(cfg.get("response_kind") or "") == "file"   # 导出/下载：返回文件，没有业务码
    if business_code_style and not file_response and _only_transport_assertions(asserts)             and not _asserts_business_code(asserts):
        issues.append(_issue(
            "status_only_assertion", WARNING,
            "接口用 HTTP 200 + 业务码表达结果，而这条用例只断言 HTTP 状态码——"
            "无权限、参数错误、系统异常都会被判成通过",
            "补一条业务码断言：正向用例断言 code 为成功值，异常用例断言 code 不是成功值",
            "assertions"))

    # ── 5. 标题声称的异常类型 与 实际 payload 是否一致 ──
    body = cfg.get("body")
    params = cfg.get("params")
    # 「缺少必填参数」的异常是结构性的：少了哪个字段，只看请求本身判不出来
    # ——接口有五个必填字段、删掉一个，剩下四个照样是正常值，请求体当然非空。
    # 所以不能按「请求体非空」来报。真正的问题是混了两种变异：
    # 剩余字段里塞着注入串、控制字符这类与「缺字段」无关的异常，
    # 跑出结果说不清是哪一个触发的。
    missing_field_case = bool(_MISSING_FIELD_TITLE.search(name))
    if missing_field_case and _has_anomaly(body, params):
        issues.append(_issue(
            "title_payload_mismatch", WARNING,
            f"标题声称「缺少必填参数」，但剩余字段里还带着别的异常数据："
            f"{json.dumps(body, ensure_ascii=False)[:80]}",
            "一条用例只留一个变量：缺字段就只缺字段，其余字段用正常值",
            "execution_config.body"))
    # 空值用例通常只清空**目标字段**，其余字段保持正常——那是对的，只留一个变量。
    # 原来只要还有非空字段就报，L2 变异的「空值: pageNum」因此全被误报。
    # 真正该报的是「一个空字段都没有」。
    if _EMPTY_TITLE.search(name) and isinstance(body, dict) and body:
        empty_fields = [k for k, v in body.items() if v in ("", None, [], {})]
        if not empty_fields:
            issues.append(_issue(
                "title_payload_mismatch", WARNING,
                f"标题声称测试空值，但字段均有取值：{list(body)[:5]}",
                "把目标字段置为空串/None，否则测不到空值分支",
                "execution_config.body"))

    # ── 6. 异常意图的请求里是否真的带了异常数据 ──
    #     这是分诊里「异常用例实际发送了合法请求」的生成侧成因。
    #     auth_missing / nonexistent_id 的异常体现在 header 或路径参数上，
    #     不在 body / params 里，故排除，避免误报。
    # 有些异常不体现在「取值长什么样」上，看请求判不出来：
    #   auth_missing / nonexistent_id  异常在请求头或路径参数
    #   required_missing               异常是「少了字段」
    #   type_error                     异常是类型不符（"not_a_number" 本身是个正常字符串）
    # 这几类的正确性由生成侧对着 schema 保证，不在这里凭 body 取值判。
    #   boundary（声明应当被拒）         越界是相对规范的 minimum/maximum 说的——0 对 minimum=1
    #                                  是越界，单看数值看不出来
    _STRUCTURAL_INTENTS = {"auth_missing", "nonexistent_id", "required_missing", "type_error"}
    range_boundary = str(data_type or "").strip().lower() == "boundary"
    if intent in NEGATIVE_INTENTS - _STRUCTURAL_INTENTS and not missing_field_case and not range_boundary:
        blob = _anomaly_blob(body, params)
        # 整个请求体为空本身就是一种变异：「缺少必填参数」正是这么造的。
        # 原来这里只看值，空请求被判成「没带异常数据」，误报了 270 条。
        empty_request = not body and not params
        if not empty_request and not _has_anomaly(body, params):
            issues.append(_issue(
                "negative_intent_without_anomaly", WARNING,
                f"用例意图是 {intent}，但请求数据看不出异常特征：{blob[:90]}",
                "确认非法/边界值是否真的写进了请求；否则接口会正常返回，"
                "这条用例测不到任何异常分支",
                "execution_config.body"))

    return issues


def summarize(issues_by_case: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """把多条用例的自检结果汇总成分布。"""
    from collections import Counter

    by_code = Counter()
    by_severity = Counter()
    blocked = 0
    for _cid, issues in issues_by_case.items():
        if any(i["severity"] == ERROR for i in issues):
            blocked += 1
        for i in issues:
            by_code[i["code"]] += 1
            by_severity[i["severity"]] += 1

    total = len(issues_by_case)
    clean = sum(1 for v in issues_by_case.values() if not v)
    return {
        "total_cases": total,
        "clean_cases": clean,
        "cases_with_issues": total - clean,
        "blocked_cases": blocked,
        "pass_rate": round(clean / total, 4) if total else 0,
        "by_severity": [{"severity": s, "count": n} for s, n in by_severity.most_common()],
        "by_code": [{"code": c, "count": n} for c, n in by_code.most_common()],
    }
