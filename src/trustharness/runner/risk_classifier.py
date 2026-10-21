"""
API 风险分级器

根据 method + path + summary + tags 对 API 进行风险分级：
  - low:    查询、列表、详情、字典、配置读取
  - medium: 导出、计算、校验、预览、临时生成
  - high:   新增、修改、删除、提交、审批、作废、付款、反审、批量处理
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── 风险关键词 ──────────────────────────────────────────

_HIGH_KEYWORDS = re.compile(
    r"(save|add|create|update|edit|delete|remove|submit|audit|approve|"
    r"pay|cancel|invalid|void|reverse|batch|insert|modify|"
    r"revoke|reject|confirm|settle|write|assign|transfer|"
    r"作废|删除|新增|修改|提交|审批|付款|反审|批量|撤销|驳回|确认|结算)",
    re.IGNORECASE,
)

_MEDIUM_KEYWORDS = re.compile(
    r"(export|calculate|preview|check|validate|import|upload|download|"
    r"generate|compute|verify|estimate|trial|"
    r"导出|计算|预览|校验|导入|上传|下载|试算)",
    re.IGNORECASE,
)

_LOW_KEYWORDS = re.compile(
    r"(page|list|info|detail|get|query|select|tree|options|dict|"
    r"search|find|count|stat|summary|view|read|fetch|"
    r"分页|列表|详情|查询|字典|配置|统计|搜索)",
    re.IGNORECASE,
)


def classify_risk(
    method: str,
    path: str,
    summary: str = "",
    tags: Optional[List[str]] = None,
) -> str:
    """
    对单个 API 进行风险分级。

    Returns:
        "low" | "medium" | "high"
    """
    text = f"{method} {path} {summary} {' '.join(tags or [])}"

    # 方法级别快速判断
    if method in ("DELETE", "PUT", "PATCH"):
        return "high"

    # 关键词匹配 (high 优先级最高)
    if _HIGH_KEYWORDS.search(text):
        # 但 GET 请求的 "save/delete" 路径仍可能是查询
        if method == "GET" and _LOW_KEYWORDS.search(text):
            return "low"
        return "high"

    if _MEDIUM_KEYWORDS.search(text):
        return "medium"

    if _LOW_KEYWORDS.search(text):
        return "low"

    # GET 默认低风险
    if method == "GET":
        return "low"

    # POST 无法识别时, 默认 medium
    return "medium"


def scan_swagger_risks(
    swagger_path: str,
) -> Dict[str, Any]:
    """
    扫描 Swagger 文件, 对所有 API 进行风险分级。

    Returns:
        {
            "total": int,
            "low": int, "medium": int, "high": int,
            "low_apis": [...],
            "medium_apis": [...],
            "high_apis": [...],
            "recommended_regression": [...],  # 建议优先回归的 20 个低风险接口
        }
    """
    with open(swagger_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    paths = data.get("paths", {})
    result: Dict[str, List[dict]] = {"low": [], "medium": [], "high": []}

    for path, path_info in paths.items():
        for method_lower, info in path_info.items():
            if not isinstance(info, dict):
                continue
            method = method_lower.upper()
            if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                continue

            tags = info.get("tags", ["未分类"])
            summary = info.get("summary", "")
            risk = classify_risk(method, path, summary, tags)

            api_info = {
                "method": method,
                "path": path,
                "summary": summary,
                "tags": tags,
                "risk": risk,
            }
            result[risk].append(api_info)

    total = sum(len(v) for v in result.values())

    # 建议优先回归: 从 low 中选 page/list 优先, 最多 20 个
    low_apis = result["low"]
    regression = []
    # 优先选分页
    for api in low_apis:
        if api["path"].endswith("/page"):
            regression.append(api)
            if len(regression) >= 20:
                break
    # 补充列表
    if len(regression) < 20:
        for api in low_apis:
            if api["path"].endswith("/list") and api not in regression:
                regression.append(api)
                if len(regression) >= 20:
                    break
    # 补充其他低风险
    if len(regression) < 20:
        for api in low_apis:
            if api not in regression:
                regression.append(api)
                if len(regression) >= 20:
                    break

    return {
        "total": total,
        "low": len(result["low"]),
        "medium": len(result["medium"]),
        "high": len(result["high"]),
        "low_apis": result["low"],
        "medium_apis": result["medium"],
        "high_apis": result["high"],
        "recommended_regression": regression,
    }
