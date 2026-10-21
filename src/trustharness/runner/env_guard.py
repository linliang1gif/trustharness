"""
生产环境保护守卫

根据环境名称和接口风险等级, 决定是否允许执行:
  - prod/生产环境/production: 禁止 high, 二次确认 medium, 允许 low (记录审计日志)
  - 其他环境: 全部放行
"""

import re
import logging
from datetime import datetime
from typing import Optional, Tuple

from trustharness.runner.risk_classifier import classify_risk

logger = logging.getLogger(__name__)

_PROD_PATTERN = re.compile(r"(prod|production|生产)", re.IGNORECASE)


class EnvGuard:
    """环境安全守卫"""

    @staticmethod
    def is_production(base_url: str, env_name: str = "") -> bool:
        """判断当前环境是否为生产环境"""
        text = f"{base_url} {env_name}"
        return bool(_PROD_PATTERN.search(text))

    @staticmethod
    def check_permission(
        base_url: str,
        method: str,
        path: str,
        summary: str = "",
        tags: Optional[list] = None,
        env_name: str = "",
        force: bool = False,
    ) -> Tuple[bool, str, str]:
        """
        检查是否允许执行该接口。

        Args:
            base_url: 目标服务器地址
            method: HTTP 方法
            path: 接口路径
            summary: 接口摘要
            tags: 接口标签
            env_name: 环境名称
            force: 是否强制执行 (用于 medium 二次确认)

        Returns:
            (allowed, risk_level, message)
        """
        risk = classify_risk(method, path, summary, tags)

        if not EnvGuard.is_production(base_url, env_name):
            # 非生产环境: 全部放行, 仅记录日志
            logger.info(f"[AUDIT] {env_name or base_url} | {risk} | {method} {path}")
            return True, risk, ""

        # 生产环境保护
        if risk == "high":
            msg = (
                f"当前为生产环境，为防止数据污染，已禁止执行高风险接口。"
                f"接口: {method} {path} (risk={risk})"
            )
            logger.warning(f"[BLOCKED] {base_url} | {method} {path} | {risk}")
            return False, risk, msg

        if risk == "medium":
            if not force:
                msg = (
                    f"当前为生产环境，该接口为中风险操作，需要二次确认。"
                    f"接口: {method} {path} (risk={risk})"
                    f" 请设置 force=true 确认执行。"
                )
                logger.warning(f"[CONFIRM_REQUIRED] {base_url} | {method} {path} | {risk}")
                return False, risk, msg
            else:
                logger.warning(f"[FORCE_EXEC] {base_url} | {method} {path} | {risk} | 二次确认通过")

        # low 或 medium+force: 允许执行, 记录审计日志
        logger.info(
            f"[AUDIT] PROD | {datetime.now().isoformat()} | "
            f"{method} {path} | risk={risk} | allowed"
        )
        return True, risk, ""
