"""
认证管理器

支持多种认证方式：
  - Bearer Token（JWT）
  - Basic Auth
  - API Key
  - Cookie
  - 自定义 Header

Token 存储在内存中（按环境隔离），也可持久化到 .env。
"""
import base64
import time
from typing import Any, Dict, Optional


class AuthManager:
    """认证管理器 — 管理和注入认证信息"""

    # 内存 token 存储: { env_key: { token, type, expires_at, extra } }
    _store: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def set_token(
        cls,
        token: str,
        auth_type: str = "bearer",
        env_key: str = "default",
        expires_in: int = 0,
        extra: Optional[Dict] = None,
    ):
        """
        保存 token

        Args:
            token: 认证令牌
            auth_type: bearer / basic / api_key / cookie / custom
            env_key: 环境标识（多环境隔离）
            expires_in: 过期秒数，0=不过期
            extra: 附加信息（如 header_name, cookie_name 等）
        """
        cls._store[env_key] = {
            "token": token.strip(),
            "type": auth_type,
            "expires_at": time.time() + expires_in if expires_in > 0 else 0,
            "extra": extra or {},
            "set_at": time.time(),
        }

    @classmethod
    def get_token(cls, env_key: str = "default") -> Optional[Dict[str, Any]]:
        """获取 token 信息"""
        entry = cls._store.get(env_key)
        if not entry:
            return None
        # 检查过期
        if entry["expires_at"] > 0 and time.time() > entry["expires_at"]:
            del cls._store[env_key]
            return None
        return entry

    @classmethod
    def clear_token(cls, env_key: str = "default"):
        """清除 token"""
        cls._store.pop(env_key, None)

    @classmethod
    def clear_all(cls):
        """清除所有 token"""
        cls._store.clear()

    @classmethod
    def list_envs(cls) -> Dict[str, Dict[str, Any]]:
        """列出所有环境的 token 状态（脱敏）"""
        result = {}
        now = time.time()
        for key, entry in cls._store.items():
            token = entry["token"]
            masked = token[:20] + "..." + token[-10:] if len(token) > 40 else token[:10] + "..."
            expired = entry["expires_at"] > 0 and now > entry["expires_at"]
            result[key] = {
                "type": entry["type"],
                "token_preview": masked,
                "expired": expired,
                "set_at": entry["set_at"],
            }
        return result

    # 常见的鉴权头名：环境默认请求头里也可能直接放着令牌
    COMMON_AUTH_HEADERS = {"authorization", "token", "x-token", "access-token", "x-access-token",
                           "x-auth-token", "cookie"}

    @classmethod
    def strip_auth(cls, headers: Dict[str, str], cookies: Dict[str, str],
                   env_key: str = "default") -> tuple:
        """去掉鉴权：该环境会注入的头/cookie，以及常见鉴权头名。"""
        probe_headers, probe_cookies = cls.inject_auth({}, {}, env_key)
        names = {k.lower() for k in probe_headers} | cls.COMMON_AUTH_HEADERS
        headers = {k: v for k, v in (headers or {}).items() if k.lower() not in names}
        cookies = {k: v for k, v in (cookies or {}).items() if k not in probe_cookies}
        return headers, cookies

    @classmethod
    def inject_auth(
        cls,
        headers: Dict[str, str],
        cookies: Dict[str, str],
        env_key: str = "default",
    ) -> tuple:
        """
        将认证信息注入到请求头/cookie 中

        Args:
            headers: 请求头（会被修改）
            cookies: Cookie（会被修改）
            env_key: 环境标识

        Returns:
            (headers, cookies) 注入后的结果
        """
        entry = cls.get_token(env_key)
        if not entry:
            return headers, cookies

        token = entry["token"]
        auth_type = entry["type"]
        extra = entry.get("extra", {})

        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {token}"

        elif auth_type == "basic":
            headers["Authorization"] = f"Basic {token}"

        elif auth_type == "api_key":
            header_name = extra.get("header_name", "X-API-Key")
            headers[header_name] = token

        elif auth_type == "cookie":
            cookie_name = extra.get("cookie_name", "session")
            cookies[cookie_name] = token

        elif auth_type == "custom":
            header_name = extra.get("header_name", "Authorization")
            prefix = extra.get("prefix", "")
            headers[header_name] = f"{prefix}{token}" if prefix else token

        return headers, cookies
