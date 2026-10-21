"""
AI 智能断言生成器

使用 DeepSeek 分析 Swagger API 定义，自动生成合理的断言规则。
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx
import logging

logger = logging.getLogger(__name__)


def _get_llm_config() -> dict:
    """获取 LLM 配置"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("DEFAULT_AI_MODEL", "deepseek-chat")
    if not api_key:
        raise ValueError("未配置 DEEPSEEK_API_KEY，无法使用 AI 断言生成")
    return {"api_key": api_key, "base_url": base_url.rstrip("/"), "model": model}


SYSTEM_PROMPT = """你是一位资深 API 测试工程师。给你一个 API 的 Swagger/OpenAPI 定义，你需要生成合理的测试断言。

输出格式必须是严格的 JSON 数组，每个断言对象包含：
- type: 断言类型，可选值: status_code, response_time, json_path, field_exists, field_equals, contains
- expected: 期望值
- path: JSON路径（仅 json_path/field_exists/field_equals 需要）

规则：
1. 每个 API 至少 3-5 条断言
2. 必须包含 status_code 和 response_time 断言
3. 根据响应 schema 生成 field_exists 断言检查关键字段
4. 分页接口要检查 data、total、pageNum 等字段
5. 列表接口要检查 data 是数组
6. 详情接口要检查核心业务字段
7. response_time 建议 5000ms
8. 只输出 JSON 数组，不要任何其他文字"""


def _call_llm(prompt: str) -> str:
    """调用 DeepSeek LLM"""
    config = _get_llm_config()
    url = f"{config['base_url']}/chat/completions"

    with httpx.Client(timeout=60) as client:
        resp = client.post(
            url,
            headers={
                "Authorization": f"Bearer {config['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": config["model"],
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 2000,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


def _extract_json(text: str) -> list:
    """从 LLM 输出中提取 JSON 数组"""
    # 尝试直接解析
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # 提取 ```json ... ```
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(1))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # 提取第一个 [ ... ]
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return []


def generate_assertions_for_api(
    path: str,
    method: str,
    summary: str = "",
    tags: List[str] = None,
    request_schema: dict = None,
    response_schema: dict = None,
) -> List[Dict[str, Any]]:
    """
    为单个 API 生成智能断言

    Args:
        path: API 路径
        method: HTTP 方法
        summary: API 摘要
        tags: 标签
        request_schema: 请求体 schema
        response_schema: 响应 schema

    Returns:
        断言列表
    """
    prompt = f"""API 定义：
- 路径: {method} {path}
- 摘要: {summary or '无'}
- 标签: {', '.join(tags or ['未分类'])}
"""
    if request_schema:
        req_str = json.dumps(request_schema, ensure_ascii=False)
        if len(req_str) > 1000:
            req_str = req_str[:1000] + "..."
        prompt += f"- 请求体 Schema:\n{req_str}\n"

    if response_schema:
        resp_str = json.dumps(response_schema, ensure_ascii=False)
        if len(resp_str) > 1000:
            resp_str = resp_str[:1000] + "..."
        prompt += f"- 响应 Schema:\n{resp_str}\n"

    prompt += "\n请生成断言 JSON 数组："

    try:
        raw = _call_llm(prompt)
        assertions = _extract_json(raw)
        # 验证格式
        valid = []
        for a in assertions:
            if isinstance(a, dict) and "type" in a:
                valid.append({
                    "type": a["type"],
                    "expected": a.get("expected"),
                    "path": a.get("path", ""),
                })
        return valid if valid else _fallback_assertions(path)
    except Exception as e:
        logger.info(f"  [AI断言] LLM调用失败({e})，使用规则回退")
        return _fallback_assertions(path)


def generate_assertions_batch(
    apis: List[Dict[str, Any]],
    max_apis: int = 10,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    批量生成断言（多个 API 一次性发给 LLM）

    Args:
        apis: API 列表 [{path, method, summary, tags, request_schema, response_schema}]
        max_apis: 单次最多处理的 API 数

    Returns:
        { "METHOD /path": [assertions...], ... }
    """
    if not apis:
        return {}

    apis = apis[:max_apis]

    # 构建批量 prompt
    prompt = f"请为以下 {len(apis)} 个 API 分别生成断言。\n\n"
    prompt += "输出格式：JSON 对象，key 为 'METHOD /path'，value 为断言数组。\n\n"

    for i, api in enumerate(apis):
        path = api.get("path", "")
        method = api.get("method", "POST")
        summary = api.get("summary", "")
        tags = api.get("tags", [])
        prompt += f"API {i+1}: {method} {path} - {summary} [{', '.join(tags)}]\n"
        req_schema = api.get("request_schema")
        if req_schema:
            s = json.dumps(req_schema, ensure_ascii=False)
            if len(s) > 500:
                s = s[:500] + "..."
            prompt += f"  请求: {s}\n"

    prompt += "\n请输出 JSON 对象："

    try:
        raw = _call_llm(prompt)
        # 提取 JSON 对象
        result = {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                result = parsed
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group())
                    if isinstance(parsed, dict):
                        result = parsed
                except json.JSONDecodeError:
                    pass

        # 验证并清洗
        cleaned = {}
        for key, assertions in result.items():
            if isinstance(assertions, list):
                valid = []
                for a in assertions:
                    if isinstance(a, dict) and "type" in a:
                        valid.append({
                            "type": a["type"],
                            "expected": a.get("expected"),
                            "path": a.get("path", ""),
                        })
                if valid:
                    cleaned[key] = valid

        # 补充没有生成到的 API
        for api in apis:
            key = f"{api['method']} {api['path']}"
            if key not in cleaned:
                cleaned[key] = _fallback_assertions(api["path"])

        return cleaned

    except Exception as e:
        logger.info(f"  [AI断言] 批量LLM调用失败({e})，使用规则回退")
        result = {}
        for api in apis:
            key = f"{api['method']} {api['path']}"
            result[key] = _fallback_assertions(api["path"])
        return result


def _fallback_assertions(path: str) -> List[Dict[str, Any]]:
    """规则回退：当 LLM 不可用时使用"""
    assertions = [
        {"type": "status_code", "expected": 200, "path": ""},
        {"type": "response_time", "expected": 5000, "path": ""},
    ]
    if "/page" in path:
        assertions.append({"type": "field_exists", "expected": None, "path": "data"})
        assertions.append({"type": "field_exists", "expected": None, "path": "data.total"})
    elif "/list" in path:
        assertions.append({"type": "field_exists", "expected": None, "path": "data"})
    elif "/info" in path or "/detail" in path:
        assertions.append({"type": "field_exists", "expected": None, "path": "data"})
    return assertions
