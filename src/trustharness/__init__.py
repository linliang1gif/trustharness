# -*- coding: utf-8 -*-
"""trustharness —— 测试可信度评测 harness。

回答一个通过率回答不了的问题：**这套测试跑出来的结论，有多可信？**

一套用例可以 100% 通过而什么都没验证到。例如，异常用例只断言
HTTP 2xx 时，即使目标错误分支没有执行，结果也可能显示为通过。

本包不替换你的测试工具。你继续用 Postman、pytest、newman、
或任何执行器——trustharness 叠加在上面，给出一个可复现、
可跨项目横向比较的信任度评分。

用法：

    from trustharness import inspect_cases, triage_failures, trust_score

    issues = inspect_cases(cases)
    verdicts = triage_failures(results)
    score = trust_score(selfcheck_results=issues, triage_results=verdicts)
    print(score.grade, score.overall)

或命令行：

    trustharness run --spec swagger.json
    trustharness score --cases cases.json --results results.json
"""
from trustharness.conventions import (
    BUSINESS_CODE,
    HTTP_STATUS,
    MIXED,
    UNKNOWN,
    ErrorConvention,
    build_assertions,
    default_convention,
    infer_from_spec,
    learn_from_history,
    merge,
    rewrite_assertions,
)
from trustharness.score import TrustScore, compute as trust_score
from trustharness.selfcheck import (
    ERROR,
    WARNING,
    check_case,
    summarize as summarize_selfcheck,
)
# 注意：不要在此再导出名为 triage 的函数。
# 它会遮蔽同名子模块 trustharness.triage，导致
# `import trustharness.triage as m` 拿到的是函数而不是模块。
# 单条分诊请用 `from trustharness.triage import triage`；
# 批量入口是下方的 triage_failures。
from trustharness.triage import (
    CATEGORY_LABELS,
    CATEGORY_OWNER,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # 用例自检
    "check_case", "inspect_cases", "summarize_selfcheck", "ERROR", "WARNING",
    # 失败分诊
    "triage_failures", "CATEGORY_LABELS", "CATEGORY_OWNER",
    # 错误约定
    "ErrorConvention", "infer_from_spec", "learn_from_history", "merge",
    "build_assertions", "rewrite_assertions", "default_convention",
    "BUSINESS_CODE", "HTTP_STATUS", "MIXED", "UNKNOWN",
    # 评分
    "trust_score", "TrustScore",
]


def inspect_cases(cases):
    """批量自检。

    cases —— 可迭代的用例字典，字段见 check_case。
             支持两种形态：{"id", "title", "data_type", "execution_config",
             "assertions"}，或执行器风格的扁平结构
             {"method", "path", "body", "params", ...}。

    返回 {case_id: issues}。
    """
    results = {}
    for index, case in enumerate(cases or []):
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id") or case.get("case_id") or index)
        config = case.get("execution_config")
        if config is None:
            # 执行器风格：字段散在顶层
            config = {
                "method": case.get("method"),
                "url": case.get("url") or case.get("path"),
                "body": case.get("body"),
                "params": case.get("params"),
                "headers": case.get("headers"),
            }
        results[case_id] = check_case(
            case_id=case_id,
            title=case.get("title"),
            data_type=case.get("data_type") or case.get("intent"),
            execution_config=config,
            assertions=case.get("assertions"),
            # 必须传：它是「应当成功还是应当被拒」的权威声明。只看 data_type
            expected_behavior=case.get("expected_behavior"),
        )
    return results


def triage_failures(results, environment_usable=None):
    """批量分诊执行结果。

    results —— 可迭代的执行结果字典，含 status / error_message /
               response_snapshot / assertion_details / data_type /
               expected_behavior。只有 status 为 failed / error 的会被分诊。
    environment_usable —— **整轮**的判定，来自 trustharness.evidence.scan。
               单条结果推不出来：令牌过期时所有请求拿同一个鉴权错误，
               逐条看的话每条负向用例都像「断言写错了」。传 False 时
               鉴权类失败归环境。None（默认）保持原行为。

    返回 [verdict, ...]，顺序与输入中的失败项一致。
    """
    verdicts = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").lower()
        if status not in ("failed", "error"):
            continue
        from trustharness.triage import triage as _triage_one

        verdict = _triage_one(
            error_message=item.get("error_message"),
            error_type=item.get("error_type"),
            response_snapshot=item.get("response_snapshot") or item.get("response"),
            assertion_details=item.get("assertion_details") or item.get("assertions"),
            data_type=item.get("data_type") or item.get("intent"),
            expected_behavior=item.get("expected_behavior"),
            status=status,
            environment_usable=environment_usable,
        )
        verdict["case_id"] = item.get("case_id") or item.get("test_case_id") or item.get("id")
        verdicts.append(verdict)
    return verdicts
