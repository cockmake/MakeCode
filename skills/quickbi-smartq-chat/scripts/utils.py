# -*- coding: utf-8 -*-
"""
QBI 小Q问数公共工具（统一版）。

提供配置读取、OpenAPI 签名、HTTP 请求（含 SSE 流式）、SSE 事件解析、
用户自动注册、试用提示以及 multipart 文件上传能力。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import string
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
from urllib import parse

import requests

from config_loader import (
    load_config as read_config,    # 向后兼容：其他脚本 from utils import read_config
    persist_to_skill_config,
    persist_to_global_config,
    persist_to_default_config,
    get_server_domain,
    check_trial_expired,
    TRIAL_EXPIRED_CODE,
    DEFAULT_CONFIG_PATH,
    GLOBAL_CONFIG_PATH,
    SKILL_CONFIG_PATH,
    _GLOBAL_ONLY_KEYS,
)

BASE_DIR = Path(__file__).resolve().parent


def require_user_id(config: dict) -> str:
    """获取 userId，按优先级：外部config → 自动注册。"""
    user_id = config.get("user_token")
    if user_id is None or str(user_id).strip() == "":
        user_id = _auto_provision_user(config)
        config["user_token"] = user_id
    else:
        user_id = str(user_id).strip()
        config["user_token"] = user_id
        # 已有 user_token（来自全局/skill/环境变量），无需再持久化
    return user_id


# ---------------------------------------------------------------------------
# 用户自动注册
# ---------------------------------------------------------------------------

from device_id import get_device_account_id as _get_device_account_id  # noqa: E402
from device_id import get_device_hostname as _get_device_hostname  # noqa: E402

_ALREADY_IN_ORG_CODE = "AE0150100022"
_NICK_EXISTS_CODE = "AE0150100010"
_last_add_user_code: Optional[str] = None


def _add_user_to_org(account_id: str, hostname: str, config: dict) -> Optional[str]:
    """
    调用 POST /openapi/v2/organization/user/addSuer 添加用户到默认组织。
    返回系统分配的 userId，失败返回 None。
    """
    uri = "/openapi/v2/organization/user/addSuer"
    body: Dict[str, Any] = {
        "accountId": account_id,
        "accountName": hostname,
        "nickName": hostname
    }
    print(f"[用户注册][添加用户] 请求: POST {uri}", flush=True)
    print(f"[用户注册][添加用户] 入参: {json.dumps(body, ensure_ascii=False)}", flush=True)
    global _last_add_user_code
    try:
        resp = request_openapi(
            "POST",
            uri,
            json_body=body,
            config=config,
        )
        result = resp.json()
        print(f"[用户注册][添加用户] 响应: {json.dumps(result, ensure_ascii=False)}", flush=True)
        _last_add_user_code = str(result.get("code", ""))
        if result.get("success") is True and isinstance(result.get("data"), dict):
            user_id = result["data"].get("userId")
            if user_id:
                return user_id
    except Exception as e:
        print(f"[用户注册][添加用户] 异常: {e}", flush=True)
    return None


def _query_user_by_account(account_name: str, config: dict) -> Optional[str]:
    """
    通过 GET /openapi/v2/organization/user/queryByAccount 查询已存在用户的 userId。
    """
    uri = "/openapi/v2/organization/user/queryByAccount"
    params = {"account": account_name}
    print(f"[用户注册][查询用户] 请求: GET {uri}?account={account_name}", flush=True)
    try:
        resp = request_openapi("GET", uri, params=params, config=config)
        result = resp.json()
        print(f"[用户注册][查询用户] 响应: {json.dumps(result, ensure_ascii=False)}", flush=True)
        if result.get("success") and isinstance(result.get("data"), dict):
            return result["data"].get("userId")
    except Exception as e:
        print(f"[用户注册][查询用户] 异常: {e}", flush=True)
    return None


def _persist_user_id(user_id: str):
    """将自动注册产生的 user_token 持久化到包内默认配置 default_config.yaml。

    仅写入 default_config.yaml，不写入全局配置或 skill 级配置，
    避免污染用户手动管理的配置文件。
    同时清理 skill 级配置中残留的全局专属键。
    """
    # --- 写入包内默认配置 ---
    try:
        persist_to_default_config("user_token", user_id)
        print(f"[用户注册] user_token 已写入 {DEFAULT_CONFIG_PATH}", flush=True)
    except Exception as e:
        print(f"[用户注册] 警告：无法将 user_token 写入 {DEFAULT_CONFIG_PATH}: {e}", flush=True)

    # --- 清理 skill 级配置中的全局专属键 ---
    _clean_global_keys_from_skill_config()


def _clean_global_keys_from_skill_config():
    """从 skill 级配置中移除所有全局配置专属键（防御性清理）。

    server_domain / api_key / api_secret / user_token 应统一由全局配置管理。
    如果 skill 级配置中残留了这些键，由于其优先级高于全局配置，
    会导致全局配置中的更新无法生效。
    本函数与 config_loader.py 中的 _resolve_global_key_conflicts 形成双重保障。
    """
    if not SKILL_CONFIG_PATH.exists():
        return
    try:
        key_prefixes = tuple(f"{k}:" for k in _GLOBAL_ONLY_KEYS)
        with open(SKILL_CONFIG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines = [line for line in lines if not line.lstrip().startswith(key_prefixes)]

        if len(new_lines) != len(lines):
            with open(SKILL_CONFIG_PATH, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            removed_count = len(lines) - len(new_lines)
            print(f"[配置] 已从 {SKILL_CONFIG_PATH} 中移除 {removed_count} 个全局专属键", flush=True)
    except Exception as e:
        print(f"[配置] 警告：无法清理 skill 级全局专属键: {e}", flush=True)


def _auto_provision_user(config: dict) -> str:
    """
    未配置 user_token 时的自动注册流程：
    1. 生成 accountId（设备 MAC 地址 MD5）和 accountName（主机名）
    2. 先通过 accountName 查询用户是否已在组织中，已存在则直接复用 userId
    3. 不存在则调用 addUser 添加到组织
    4. 将 userId 固化到包内 default_config.yaml
    """
    account_id = _get_device_account_id()
    hostname = _get_device_hostname()
    print(f"[用户注册] 未配置 user_token，开始自动注册 (accountId={account_id}, accountName={hostname})", flush=True)

    existing_uid = _query_user_by_account(hostname, config)
    if existing_uid:
        print(f"[用户注册] 通过 accountName 查询到已有用户，userId={existing_uid}", flush=True)
        _persist_user_id(existing_uid)
        return existing_uid

    print(f"[用户注册] 未查询到已有用户，正在添加 (accountName={hostname}) ...", flush=True)
    uid = _add_user_to_org(account_id, hostname, config)
    if uid:
        print(f"[用户注册] 添加成功，userId={uid}", flush=True)
        _persist_user_id(uid)
        return uid

    if _last_add_user_code in (_ALREADY_IN_ORG_CODE, _NICK_EXISTS_CODE):
        print(f"[用户注册] 添加返回已存在（错误码={_last_add_user_code}），重新查询 userId ...", flush=True)
        queried_uid = _query_user_by_account(hostname, config)
        if queried_uid:
            print(f"[用户注册] 查询成功，userId={queried_uid}", flush=True)
            _persist_user_id(queried_uid)
            return queried_uid

    suffixed_name = f"{hostname}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=5))}"
    print(f"[用户注册] 使用带后缀名称重试 (accountName={suffixed_name}) ...", flush=True)
    uid = _add_user_to_org(account_id, suffixed_name, config)
    if uid:
        print(f"[用户注册] 重试添加成功，userId={uid}", flush=True)
        _persist_user_id(uid)
        return uid

    raise ValueError(
        "自动注册用户失败，请手动在 ~/.qbi/config.yaml 中配置 user_token。"
        "可通过 Quick BI 管理控制台获取用户 ID。"
    )


# ---------------------------------------------------------------------------
# OpenAPI 签名
# ---------------------------------------------------------------------------

def build_signature(
    method: str,
    uri: str,
    params: Optional[Dict[str, Any]],
    access_id: str,
    access_key: str,
    nonce: str,
    timestamp: str,
) -> str:
    if not params:
        request_query_string = ""
    else:
        parts: List[str] = []
        for key in sorted(params):
            value = params[key]
            if value is None or value == "":
                continue
            parts.append(f"{key}={value}")
        request_query_string = "\n" + "&".join(parts) if parts else ""

    request_headers = (
        "\nX-Gw-AccessId:" + access_id
        + "\nX-Gw-Nonce:" + nonce
        + "\nX-Gw-Timestamp:" + timestamp
    )
    string_to_sign = method.upper() + "\n" + uri + request_query_string + request_headers
    encoded_string = parse.quote(string_to_sign, "")
    digest = hmac.new(
        access_key.encode("utf-8"),
        encoded_string.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_request_headers(
    method: str,
    uri: str,
    params: Optional[Dict[str, Any]],
    *,
    content_type: Optional[str] = None,
    config: Optional[dict] = None,
) -> Dict[str, str]:
    config = config or read_config()
    access_id = str(config["api_key"])
    access_key = str(config["api_secret"])
    nonce = str(uuid.uuid4())
    timestamp = str(int(time.time() * 1000))

    signature = build_signature(method, uri, params, access_id, access_key, nonce, timestamp)

    headers = {
        "X-Gw-AccessId": access_id,
        "X-Gw-Nonce": nonce,
        "X-Gw-Timestamp": timestamp,
        "X-Gw-Signature": signature,
        "X-Gw-Debug": "true",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


# ---------------------------------------------------------------------------
# HTTP 请求
# ---------------------------------------------------------------------------

def request_openapi(
    method: str,
    uri: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    form_data: Optional[Dict[str, Any]] = None,
    sign_params: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
    config: Optional[dict] = None,
) -> requests.Response:
    """调用 Quick BI OpenAPI（非流式）。"""
    config = config or read_config()
    server_domain = get_server_domain(config)
    method = method.upper()
    url = server_domain + uri

    if sign_params is None and method == "GET":
        sign_params = params
    if sign_params is None and form_data is not None:
        sign_params = form_data

    content_type: Optional[str] = None
    if json_body is not None:
        content_type = "application/json"
    elif form_data is not None:
        content_type = "application/x-www-form-urlencoded"

    headers = build_request_headers(method, uri, sign_params, content_type=content_type, config=config)

    kwargs: Dict[str, Any] = {"method": method, "url": url, "headers": headers, "timeout": timeout}

    if method == "GET":
        kwargs["params"] = params
    elif json_body is not None:
        kwargs["json"] = json_body
    elif form_data is not None:
        kwargs["data"] = form_data
    else:
        kwargs["params"] = params

    resp = requests.request(**kwargs)
    if not resp.ok:
        body = ""
        try:
            body = resp.text[:2000]
        except Exception:
            pass
        check_trial_expired(body)
        raise requests.HTTPError(
            f"HTTP {resp.status_code} {resp.reason} for {method} {uri}\n响应体: {body}",
            response=resp,
        )
    return resp


def request_openapi_stream(
    uri: str,
    *,
    json_body: Dict[str, Any],
    config: Optional[dict] = None,
    timeout: int = 600,
) -> Generator[str, None, None]:
    """
    POST 流式请求，返回 SSE 事件文本块的生成器。
    每次 yield 一个完整的 SSE 事件块（以 ``\\n\\n`` 分隔）。
    """
    config = config or read_config()
    server_domain = get_server_domain(config)
    url = server_domain + uri

    headers = build_request_headers("POST", uri, None, content_type="application/json", config=config)
    headers["origin"] = server_domain
    headers["Accept"] = "text/event-stream"
    headers["Accept-Encoding"] = "identity"
    headers["Cache-Control"] = "no-cache"

    with requests.post(url, json=json_body, headers=headers, stream=True, timeout=timeout) as resp:
        if not resp.ok:
            body = ""
            try:
                body = resp.text[:2000]
            except Exception:
                pass
            check_trial_expired(body)
            raise requests.HTTPError(
                f"HTTP {resp.status_code} {resp.reason} for POST {uri}\n响应体: {body}",
                response=resp,
            )
        resp.encoding = "utf-8"
        buffer = ""
        for chunk in resp.iter_content(chunk_size=1, decode_unicode=True):
            if chunk:
                buffer += chunk.replace("\r\n", "\n")
                while "\n\n" in buffer:
                    event_block, buffer = buffer.split("\n\n", 1)
                    event_block = event_block.strip()
                    if event_block:
                        yield event_block
        if buffer.strip():
            yield buffer.strip()


# ---------------------------------------------------------------------------
# SSE 事件解析
# ---------------------------------------------------------------------------

def parse_sse_event(raw_event: str) -> Dict[str, Any]:
    """
    解析单个 SSE 事件块，返回 data 中的 JSON 字典。

    事件格式示例::

        event:message
        data:{"data":"xxx","type":"reasoning"}
    """
    lines = raw_event.strip().split("\n")
    data_content = ""
    for line in lines:
        if line.startswith("data:"):
            data_content = line[len("data:"):]
            break

    if not data_content:
        return {}

    try:
        return json.loads(data_content)
    except json.JSONDecodeError:
        try:
            repaired = data_content.replace('\\"', '"').replace('\\\\', '\\')
            return json.loads(repaired)
        except json.JSONDecodeError:
            return {"raw": data_content}
