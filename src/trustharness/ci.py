# -*- coding: utf-8 -*-
"""CI 产物：JUnit XML 与 Markdown 摘要。零依赖。

GitLab（artifacts:reports:junit）、Jenkins、各类 GitHub test reporter 都认
JUnit XML。但「通过 / 失败」那套不能原样照搬，否则 CI 页面上看到的还是通过率：

- 判了通过、请求却没走到业务逻辑 → skipped，不是 passed。那次令牌过期的执行里
  2839 条「通过」全是这种，照搬过去 CI 上就是一片绿
- 断言写错、用例无效 → skipped，并标「测试缺陷」。它们不该让被测系统背锅，
  也不能藏起来
- 环境不可达、鉴权失效 → error。不是被测系统的缺陷，但这轮结果不作数
- 疑似真实缺陷、证据不足需人工复核 → failure

另有一个 trust-gate 套件，放执行有效性和信任度阈值两道门禁——
CI 该不该红，看的是它们，不是逐条用例。
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional

from trustharness.evidence import AUTH_REJECTED, DEGRADED, GATEWAY_ERROR, NO_RESPONSE, UNUSABLE
from trustharness.selfcheck import ERROR
from trustharness.triage import (
    AUTH_EXPIRED,
    BAD_ASSERTION,
    CATEGORY_LABELS,
    CATEGORY_OWNER,
    ENV_UNAVAILABLE,
    INVALID_CASE,
)

GATE_SUITE = "trust-gate"
EXECUTION_SUITE = "execution"
SELFCHECK_SUITE = "selfcheck"

# 失败分诊类别 → JUnit 结果
_ERROR_CATEGORIES = {ENV_UNAVAILABLE, AUTH_EXPIRED}          # 这轮不作数
_TEST_DEFECT_CATEGORIES = {BAD_ASSERTION, INVALID_CASE}      # 错在测试自己

_BLOCKED_TEXT = {
    AUTH_REJECTED: "卡在鉴权层",
    GATEWAY_ERROR: "卡在网关层",
    NO_RESPONSE: "没拿到响应",
}

# XML 1.0 不允许的控制字符。响应体原样塞进报告时经常带着，
# 不清掉的话整份 XML 解析失败——CI 那边只会显示「报告无效」
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _clean(text: Any, limit: int = 4000) -> str:
    return _INVALID_XML.sub("", str(text if text is not None else ""))[:limit]


def _case_key(case: Dict[str, Any], index: int) -> str:
    """用例的 id，取法与 inspect_cases 一致。"""
    return str(case.get("id") or case.get("case_id") or index)


def _row_key(row: Dict[str, Any], index: int) -> str:
    """执行结果指向的用例 id，取法与 triage_failures 一致。

    两者顺序不同是故意的：执行结果里的 id 常常是「这一次执行」的 id，
    case_id / test_case_id 才是用例。取错了，失败就对不上它的分诊结论。
    """
    return str(row.get("case_id") or row.get("test_case_id") or row.get("id") or index)


def _case_name(case_id: str, case: Optional[Dict[str, Any]]) -> str:
    title = (case or {}).get("title")
    return _clean(f"{case_id} {title}" if title else case_id, 300)


def _testcase(parent: ET.Element, classname: str, name: str) -> ET.Element:
    return ET.SubElement(parent, "testcase", classname=classname, name=name, time="0")


def _mark(testcase: ET.Element, kind: str, message: str, detail: str = "",
          type_: str = "") -> None:
    child = ET.SubElement(testcase, kind, message=_clean(message, 500))
    if type_:
        child.set("type", type_)
    if detail:
        child.text = _clean(detail)


def _gate_suite(root: ET.Element, *, score, execution: Optional[Dict[str, Any]],
                min_score: Optional[float]) -> None:
    suite = ET.SubElement(root, "testsuite", name=GATE_SUITE)
    classname = f"trustharness.{GATE_SUITE}"

    case = _testcase(suite, classname, "execution-validity")
    if not execution or execution.get("validity") is None:
        _mark(case, "skipped", "没有可依据的正向请求，不评估请求是否到达被测系统")
    elif execution.get("verdict") == UNUSABLE:
        _mark(case, "failure", execution.get("headline", "这次执行不算数"),
              f"请求真正到达 {execution.get('validity_percent')}%；"
              f"by_outcome={execution.get('by_outcome')}", type_=UNUSABLE)
    elif execution.get("verdict") == DEGRADED:
        ET.SubElement(case, "system-out").text = _clean(execution.get("headline"))

    case = _testcase(suite, classname, "trust-score")
    overall = getattr(score, "overall", None)
    detail = "\n".join([f"信任度 {overall}（{getattr(score, 'grade', 'N/A')}）",
                        *[f"· {c}" for c in (getattr(score, "caveats", None) or [])]])
    if min_score is None:
        ET.SubElement(case, "system-out").text = _clean(detail + "\n未设置阈值，不做门禁")
    elif overall is None or overall < min_score:
        _mark(case, "failure", f"信任度 {overall} 低于阈值 {min_score}", detail,
              type_="below-threshold")
    else:
        ET.SubElement(case, "system-out").text = _clean(detail)


def _execution_suite(root: ET.Element, *, cases: List[Dict[str, Any]],
                     selfcheck: Dict[str, List[Dict[str, Any]]],
                     rows: List[Dict[str, Any]],
                     verdicts: List[Dict[str, Any]]) -> None:
    from trustharness.evidence import records_from_results

    suite = ET.SubElement(root, "testsuite", name=EXECUTION_SUITE)
    classname = f"trustharness.{EXECUTION_SUITE}"
    by_case = {_case_key(c, i): c for i, c in enumerate(cases) if isinstance(c, dict)}
    by_verdict = {str(v.get("case_id")): v for v in verdicts}

    seen = set()
    records = records_from_results(rows)
    for index, (row, record) in enumerate(zip(
            [r for r in rows if isinstance(r, dict)], records)):
        case_id = _row_key(row, index)
        seen.add(case_id)
        testcase = _testcase(suite, classname, _case_name(case_id, by_case.get(case_id) or row))
        status = str(row.get("status") or "").lower()

        if status == "passed":
            blocked = _BLOCKED_TEXT.get(record.get("outcome"))
            # 「没带令牌应当被拒」的用例拿到鉴权错误，是测到了它要测的东西
            if record.get("outcome") == AUTH_REJECTED and record.get("expects_auth_rejection"):
                blocked = None
            if blocked:
                _mark(testcase, "skipped",
                      f"判了通过，但请求{blocked}——没有验证任何东西")
            continue

        if status in ("failed", "error"):
            verdict = by_verdict.get(case_id) or {}
            category = verdict.get("category")
            label = CATEGORY_LABELS.get(category, category or "未分诊")
            detail = "\n".join(filter(None, [
                f"归属：{CATEGORY_OWNER.get(category, '未知')}",
                f"原因：{verdict.get('reason')}" if verdict.get("reason") else "",
                f"建议：{verdict.get('suggestion')}" if verdict.get("suggestion") else "",
                ("证据：" + json.dumps(verdict["evidence"], ensure_ascii=False, default=str))
                if verdict.get("evidence") else "",
            ]))
            if category in _ERROR_CATEGORIES:
                _mark(testcase, "error", f"{label}：{verdict.get('reason', '')}", detail,
                      type_=category)
            elif category in _TEST_DEFECT_CATEGORIES:
                _mark(testcase, "skipped", f"[测试缺陷] {label}：{verdict.get('reason', '')}")
                ET.SubElement(testcase, "system-out").text = _clean(detail)
            else:
                _mark(testcase, "failure", f"{label}：{verdict.get('reason', '')}", detail,
                      type_=category or "untriaged")
            continue

        _mark(testcase, "skipped", f"未执行（{status or '无状态'}）")

    # 用例文件里有、却没有执行结果的：自检拦下的，或者压根没跑
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            continue
        case_id = _case_key(case, index)
        if case_id in seen:
            continue
        testcase = _testcase(suite, classname, _case_name(case_id, case))
        codes = [i.get("code") for i in selfcheck.get(case_id, []) if i.get("severity") == ERROR]
        _mark(testcase, "skipped",
              f"自检拦截，未进入执行：{', '.join(codes)}" if codes else "没有执行结果")


def _selfcheck_suite(root: ET.Element, *, cases: List[Dict[str, Any]],
                     selfcheck: Dict[str, List[Dict[str, Any]]]) -> None:
    suite = ET.SubElement(root, "testsuite", name=SELFCHECK_SUITE)
    classname = f"trustharness.{SELFCHECK_SUITE}"
    by_case = {_case_key(c, i): c for i, c in enumerate(cases) if isinstance(c, dict)}
    for case_id, issues in selfcheck.items():
        testcase = _testcase(suite, classname, _case_name(case_id, by_case.get(case_id)))
        errors = [i for i in issues if i.get("severity") == ERROR]
        text = "\n".join(f"[{i.get('severity')}] {i.get('code')}：{i.get('message')}"
                         for i in issues)
        if errors:
            _mark(testcase, "failure", "；".join(i.get("message", "") for i in errors), text,
                  type_=errors[0].get("code", "selfcheck"))
        elif issues:
            ET.SubElement(testcase, "system-out").text = _clean(text)


def _count(suite: ET.Element) -> None:
    cases = suite.findall("testcase")
    suite.set("tests", str(len(cases)))
    for kind, attr in (("failure", "failures"), ("error", "errors"), ("skipped", "skipped")):
        suite.set(attr, str(sum(1 for c in cases if c.find(kind) is not None)))
    suite.set("time", "0")


def junit_xml(*, score, cases: Iterable[Dict[str, Any]],
              selfcheck: Dict[str, List[Dict[str, Any]]],
              rows: Optional[List[Dict[str, Any]]] = None,
              verdicts: Optional[List[Dict[str, Any]]] = None,
              execution: Optional[Dict[str, Any]] = None,
              min_score: Optional[float] = None) -> str:
    """生成 JUnit XML。rows 为空时只有静态结论：逐条列自检结果。"""
    cases = list(cases or [])
    root = ET.Element("testsuites", name="trustharness")
    _gate_suite(root, score=score, execution=execution, min_score=min_score)
    if rows:
        _execution_suite(root, cases=cases, selfcheck=selfcheck, rows=rows,
                         verdicts=verdicts or [])
    else:
        _selfcheck_suite(root, cases=cases, selfcheck=selfcheck)

    for suite in root.findall("testsuite"):
        _count(suite)
    for attr in ("tests", "failures", "errors", "skipped"):
        root.set(attr, str(sum(int(s.get(attr)) for s in root.findall("testsuite"))))

    if hasattr(ET, "indent"):           # 3.9+
        ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def gate_passed(score, min_score: Optional[float]) -> bool:
    if min_score is None:
        return True
    overall = getattr(score, "overall", None)
    return overall is not None and overall >= min_score


def markdown_summary(*, score, execution: Optional[Dict[str, Any]] = None,
                     verdicts: Optional[List[Dict[str, Any]]] = None,
                     notes: Optional[List[str]] = None,
                     min_score: Optional[float] = None) -> str:
    """给人看的一页：GitHub 的 $GITHUB_STEP_SUMMARY、GitLab 的 MR 评论都能直接贴。"""
    from collections import Counter

    overall = getattr(score, "overall", None)
    grade = getattr(score, "grade", "N/A")
    passed = gate_passed(score, min_score)
    verdict_text = ("" if min_score is None
                    else ("· 门禁通过" if passed else f"· **门禁未通过**（阈值 {min_score}）"))

    lines = [f"## 信任度 {overall if overall is not None else 'N/A'}（{grade}）{verdict_text}", ""]
    if execution and execution.get("verdict") in (UNUSABLE, DEGRADED):
        lines += [f"> **{_clean(execution.get('headline'), 300)}**", ""]

    lines += ["| 维度 | 分数 | 依据 |", "|---|---:|---|"]
    for dim in getattr(score, "dimensions", None) or []:
        value = "—" if dim.score is None else f"{dim.score:.1f}"
        lines.append(f"| {dim.label} | {value} | {_clean(dim.detail, 200)} |")
    if execution and execution.get("validity_percent") is not None:
        lines.append(f"| 请求真正到达 | {execution['validity_percent']}% | "
                     f"{execution.get('positive_reached')}/{execution.get('positive_cases')} "
                     "条正向请求走到了业务逻辑（封顶用，不参与加权） |")
    raw = getattr(score, "raw_overall", None)
    if raw is not None and overall is not None and raw != overall:
        lines += ["", f"封顶前 {raw}：分数被执行有效性压到了 {overall}。"]

    if verdicts:
        counts = Counter(v.get("category") for v in verdicts)
        lines += ["", f"**失败 {len(verdicts)} 条，该找谁：**", "",
                  "| 归属 | 类别 | 条数 |", "|---|---|---:|"]
        for category, count in counts.most_common():
            lines.append(f"| {CATEGORY_OWNER.get(category, '未知')} | "
                         f"{CATEGORY_LABELS.get(category, category)} | {count} |")

    caveats = list(getattr(score, "caveats", None) or []) + list(notes or [])
    if caveats:
        lines += ["", "**需要注意：**", ""]
        lines += [f"- {_clean(c, 300)}" for c in dict.fromkeys(caveats)]
    return "\n".join(lines) + "\n"
