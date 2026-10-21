"""
OAuth2 Token 自动获取器

支持 password grant_type。
带内存缓存和自动刷新：Token 过期前 60 秒自动重新获取。

配置示例（auth_config）:
{
    "token_url": "https://sit-sso.example.com/oauth/token",
    "client_authorization": "<base64-client-id-and-secret>",
    "username": "<test-user>",
    "password": "<test-password>",
    "grant_type": "password",
    "scope": "all"
}
"""

import time
import httpx
from typing import Any, Dict, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


# 内存缓存: { cache_key: { access_token, token_type, expires_at } }
_token_cache: Dict[str, Dict[str, Any]] = {}

# Token 过期前提前刷新的秒数
_REFRESH_MARGIN = 60


def _cache_key(env_key: str) -> str:
    return f"oauth2_{env_key}"


def fetch_oauth2_token(auth_config: Dict[str, Any], env_key: str = "default", force: bool = False) -> Tuple[str, str]:
    """
    获取 OAuth2 Token（带缓存）

    Args:
        auth_config: OAuth2 配置字典
        env_key: 环境标识（用于缓存隔离）
        force: 是否强制刷新

    Returns:
        (access_token, token_type)

    Raises:
        Exception: 获取失败时抛出
    """
    ck = _cache_key(env_key)

    # 检查缓存
    if not force and ck in _token_cache:
        entry = _token_cache[ck]
        if entry["expires_at"] > time.time() + _REFRESH_MARGIN:
            return entry["access_token"], entry["token_type"]
        else:
            logger.info(f"🔄 OAuth2 Token 即将过期，自动刷新 (env={env_key})")

    # 构建请求
    token_url = auth_config.get("token_url", "")
    if not token_url:
        raise ValueError("OAuth2 配置缺少 token_url")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # Basic Auth for client credentials
    client_auth = auth_config.get("client_authorization", "")
    if client_auth:
        headers["Authorization"] = f"Basic {client_auth}"

    # Form body
    form_data = {
        "grant_type": auth_config.get("grant_type", "password"),
        "scope": auth_config.get("scope", "all"),
    }

    # password grant 需要 username/password
    grant_type = form_data["grant_type"]
    if grant_type == "password":
        username = auth_config.get("username", "")
        password = auth_config.get("password", "")
        if not username:
            raise ValueError("OAuth2 password grant 需要 username")
        form_data["username"] = username
        form_data["password"] = password
    elif grant_type == "client_credentials":
        # client_credentials 只需要 client_authorization
        pass

    logger.info(f"🔑 正在获取 OAuth2 Token: {token_url}")
    logger.info(f"   grant_type={grant_type}, username={form_data.get('username', 'N/A')}")
    logger.info(f"   headers: { {k: (v[:30] + '...' if len(v) > 30 else v) for k, v in headers.items()} }")
    logger.info(f"   form_data keys: {list(form_data.keys())}")

    try:
        resp = httpx.post(
            token_url,
            headers=headers,
            data=form_data,
            timeout=15,
            verify=False,
        )
    except httpx.HTTPError as e:
        raise Exception(f"OAuth2 Token 请求失败: {e}")

    # 解析响应（无论成功失败都尝试解析）
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        error_desc = data.get("error_description") or data.get("error") or data.get("message") or data.get("msg") or resp.text[:200]
        logger.info(f"❌ OAuth2 返回 {resp.status_code}: {error_desc}")
        logger.info(f"   完整响应: {resp.text[:500]}")
        raise Exception(f"OAuth2 返回 {resp.status_code}: {error_desc}")

    access_token = data.get("access_token", "")
    if not access_token:
        error_desc = data.get("error_description") or data.get("error") or data.get("message") or str(data)
        raise Exception(f"OAuth2 返回无 access_token: {error_desc}")

    token_type = data.get("token_type", "Bearer")
    expires_in = int(data.get("expires_in", 7200))

    # 缓存
    _token_cache[ck] = {
        "access_token": access_token,
        "token_type": token_type,
        "expires_at": time.time() + expires_in,
        "expires_in": expires_in,
    }

    preview = access_token[:20] + "..." if len(access_token) > 20 else access_token
    logger.info(f"✅ OAuth2 Token 获取成功! (有效期 {expires_in}s, preview={preview})")
    return access_token, token_type


def clear_oauth2_cache(env_key: str = "default"):
    """清除指定环境的 OAuth2 Token 缓存"""
    ck = _cache_key(env_key)
    _token_cache.pop(ck, None)


def clear_all_oauth2_cache():
    """清除所有 OAuth2 Token 缓存"""
    _token_cache.clear()


def get_oauth2_cache_status(env_key: str = "default") -> Optional[Dict[str, Any]]:
    """获取缓存状态"""
    ck = _cache_key(env_key)
    entry = _token_cache.get(ck)
    if not entry:
        return None
    now = time.time()
    return {
        "token_preview": entry["access_token"][:20] + "...",
        "token_type": entry["token_type"],
        "expires_in": max(0, int(entry["expires_at"] - now)),
        "expired": now > entry["expires_at"],
    }
