# -*- coding: utf-8 -*-
"""用例意图 —— 这条用例「应当成功」还是「应当被拒」。

自检、分诊、执行有效性、断言重写都要先回答这个问题，而且必须答得一样。

## 为什么只能有一份

这份词表以前有三份：selfcheck.py 一份、triage.py 一份、平台的
trust_report_service.py 又一份。三份各自漂开了，而且不报错：

- 「安全测试」只在自检那份里算负向，分诊那份不认识它
- 平台那份认 normal / 正向 / 正常 / ok / 2xx / 成功，harness 两份都不认
- 遇到认不出的 expected_behavior，平台那份直接判「非正向」，
  harness 退回去看 data_type
- boundary 没有 expected_behavior 时，平台判「非正向」，harness 判「应当成功」

同一条用例，自检、分诊、执行有效性可能得出三种意图。

还有比词表漂移更隐蔽的：调用方拿**原始 data_type** 直接去查词表，
绕过了 resolve_intent。boundary 既不在正向也不在负向里，于是落进
「意图未标注」分支——断言重写只剩一条「状态码 < 500」，什么都拦不住。

现在整个 harness 和平台只从这里取意图。要判意图，调 resolve_intent，
不要自己查词表。
"""
from __future__ import annotations

from typing import Any, Optional

# 「预期被拒绝」：接口拒绝它们才是正确行为
NEGATIVE_INTENTS = frozenset({
    # 英文（生成器写入）
    "invalid", "auth_missing", "nonexistent_id", "abnormal",
    "required_missing", "empty_value", "type_error", "invalid_enum",
    "overflow", "negative",
    # 平台 L2 变异用例缺 data_type 时的兜底值（swagger_service）
    "mutation",
    # 中文（人工、AI 生成、表格导入时写入）
    "异常测试", "安全测试", "反向", "负向", "异常",
})

# boundary / 边界测试 是**数据形态**而非预期结果：
# 「合法范围内的边界值」应当成功，「刚越界的值」应当被拒。
# 单看 data_type 分不出这两者——必须看 expected_behavior。
#
# 并据此把它们的断言从 [200,201,204] 改成
# [200,400,401,403,404,422,500] + 要求业务码为错误——
# 既几乎不再拒绝任何状态码，又要求合法边界值被拒绝，严格变差。
AMBIGUOUS_INTENTS = frozenset({"boundary", "边界测试"})

# 「预期成功」
POSITIVE_INTENTS = frozenset({
    "valid", "positive", "functional", "normal",
    "功能测试", "正向", "正常",
})

# 既非正向也非负向：意图本身不表达「应成功还是应失败」，跳过意图相关规则
NEUTRAL_INTENTS = frozenset({"manual", "ai_execution", "api", "ui", "兼容性测试"})

KNOWN_INTENTS = NEGATIVE_INTENTS | AMBIGUOUS_INTENTS | POSITIVE_INTENTS | NEUTRAL_INTENTS

# 意图本身就是「应当被鉴权拒绝」。这类用例拿到鉴权错误是测到了它要测的东西，
# 不能算「请求被挡在门外、什么都没验证」。
# 安全测试不在内：它可能测越权，也可能测注入，单看意图分不出来。
AUTH_REJECTION_INTENTS = frozenset({"auth_missing"})

# expected_behavior 是对「这条用例应该得到什么结果」的显式声明，
# 比 data_type（描述数据形态）更权威。
BEHAVIOR_TO_INTENT = {
    "success": "valid",
    "ok": "valid",
    "2xx": "valid",
    "成功": "valid",
    "client_error": "invalid",
    "server_error": "invalid",
    "error": "invalid",
    "4xx": "invalid",
    "失败": "invalid",
}


def _normalize(value: Any) -> str:
    # 枚举（如 ExpectedBehavior.SUCCESS）取 .value，否则 str() 会得到类名
    value = getattr(value, "value", value)
    return str(value).strip().lower() if value is not None else ""


def resolve_intent(data_type: Optional[str] = None,
                   expected_behavior: Optional[str] = None,
                   *, assume_ambiguous_valid: bool = True) -> str:
    """判定用例意图。

    expected_behavior 优先：它直说「应当成功」还是「应当被拒」。
    data_type 只描述数据形态，boundary 这类本身并不表达预期结果。

    assume_ambiguous_valid —— boundary 没有 expected_behavior 可依据时怎么办。
      True（默认）：按「应当成功」处理。自检、分诊、执行有效性用这个——
          误判为负向会把正确的断言指控成错的，代价比漏判大。
      False：原样返回 boundary，调用方会把它当「意图未知」。**生成断言**
          必须用这个：生成器造的边界值是 -1、0、MAX_INT，越界的那几个
          本来就该被拒，猜它「应当成功」等于亲手写一条必然失败的断言。

    对已经判定过的意图再调一次，结果不变（"valid" 仍是 "valid"），
    所以接收意图参数的函数可以放心在内部再解析一遍。
    """
    behavior = _normalize(expected_behavior)
    intent = _normalize(data_type)
    if behavior in BEHAVIOR_TO_INTENT:
        mapped = BEHAVIOR_TO_INTENT[behavior]
        # 明确的负向数据 + 明确的负向预期，保留更具体的 data_type
        if mapped == "invalid" and intent in NEGATIVE_INTENTS:
            return intent
        return mapped
    if intent in AMBIGUOUS_INTENTS and assume_ambiguous_valid:
        return "valid"
    return intent


def default_behavior(data_type: Optional[str] = None) -> Optional[str]:
    """生成器只知道 data_type 时，替它补上与之一致的 expected_behavior。

    负向意图 → "client_error"，正向意图 → "success"，其余返回 None——
    boundary 这类歧义类型、manual 这类中性类型、词表认不出的，都不替它声明。

    为什么需要：expected_behavior 比 data_type 权威。生成器不管类型一律写
    "success"，声明就会盖过类型——「异常测试」「安全测试」配上 success，
    自检、分诊、执行有效性全都把它当「应当成功」，负向用例期望成功这种
    假通过再也查不出来。
    """
    intent = resolve_intent(data_type, None, assume_ambiguous_valid=False)
    if intent in NEGATIVE_INTENTS:
        return "client_error"
    if intent in POSITIVE_INTENTS:
        return "success"
    return None


def is_positive(data_type: Optional[str] = None,
                expected_behavior: Optional[str] = None) -> bool:
    """这条用例是不是「正常访问、应当成功」的。

    执行有效性只拿这类用例判环境：正向用例不会想要鉴权错误，
    反向用例拿到鉴权错误可能完全正确。
    """
    return resolve_intent(data_type, expected_behavior) in POSITIVE_INTENTS
