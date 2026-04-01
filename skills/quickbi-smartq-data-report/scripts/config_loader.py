# -*- coding: utf-8 -*-
"""
QBI 小Q报告配置加载器。

实现四层配置分层加载，确保用户配置不受技能包更新影响。

加载优先级（低 → 高）：
1. default_config.yaml — 包内默认值，随技能包发布
2. ~/.qbi/config.yaml — QBI 全局配置，所有 skill 共享
3. ~/.qbi/smartq-data-report/config.yaml — skill 级用户配置
4. ACCESS_TOKEN 环境变量 — 最高优先级，适合容器部署
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR.parent / "default_config.yaml"

QBI_HOME = Path.home() / ".qbi"
GLOBAL_CONFIG_PATH = QBI_HOME / "config.yaml"

SKILL_NAME = "smartq-data-report"
SKILL_CONFIG_DIR = QBI_HOME / SKILL_NAME
SKILL_CONFIG_PATH = SKILL_CONFIG_DIR / "config.yaml"


# ---------------------------------------------------------------------------
# 试用凭证常量与解码器
# ---------------------------------------------------------------------------

_R0 = "MzQ5OTY5YmMtNTk2MS00NjIzLTg0ZGUtNmI4NThiZTBkYjAy"
_R1 = "YWNhOTU4MTQtNzc1MC00OTI2LTgzYjAtNjZmMmQ1ZWE3ODZi"


def _rv(v: str) -> str:
    return base64.b64decode(v).decode("utf-8")


# ---------------------------------------------------------------------------
# 试用欢迎提示
# ---------------------------------------------------------------------------

_TRIAL_WELCOME_MSG = """
============================================================
您的超级数据分析师已就位！
只需自然语言提问，即可智能匹配并分析您的 Excel 或 Quick BI 数据集，
将洞察即时呈现。让复杂分析，从未如此简单。

检测到您尚未配置凭证，我们将自动为您注册试用凭证并进入试用期。

试用到期后，请前往 Quick BI 控制台获取正式凭证：
  https://www.aliyun.com/product/quickbi-smart?utm_content=g_1000411205

如需帮助，请扫码加入交流群获取最新资讯：
  https://at.umtrack.com/r4Tnme
============================================================
""".strip()


def _print_trial_welcome():
    print(f"\n{_TRIAL_WELCOME_MSG}\n", flush=True)


# ---------------------------------------------------------------------------
# 试用到期检测
# ---------------------------------------------------------------------------

TRIAL_EXPIRED_CODE = "AE0579100004"

_TRIAL_EXPIRED_MESSAGE = """
============================================================
小 Q 超级分析助理已陪伴您一周，我们看到您在通过 AI 寻找数据背后的真相，这很了不起。

🕙 试用模式已结束
授权到期后，动态分析将暂告一段落。

💡 其实，您可以更轻松
目前的"文件模式"仍需您手动搬运数据。让 AI 直连企业存量数据资产，实现分析结果自动更新？立即体验完整功能。

🚀 0 元体验，限时加码
现在上阿里云，将额外赠送 30 天全功能体验，解锁企业级安全管控与深度分析引擎，让 AI 洞察更准、更稳。点击下方链接，领取试用：
https://www.aliyun.com/product/quickbi-smart?utm_content=g_1000411205

💬 点击下方链接，进入交流群获取最新资讯：
https://at.umtrack.com/r4Tnme
============================================================
""".strip()


def check_trial_expired(result) -> bool:
    """检查 API 响应是否包含试用到期错误码，如果是则打印提示信息。

    Args:
        result: API 响应 dict 或原始文本 str。

    Returns:
        True 表示检测到试用到期，False 表示非此错误。
    """
    code = None
    if isinstance(result, dict):
        code = str(result.get("code", ""))
    elif isinstance(result, str):
        if TRIAL_EXPIRED_CODE in result:
            code = TRIAL_EXPIRED_CODE

    if code == TRIAL_EXPIRED_CODE:
        print(f"\n{_TRIAL_EXPIRED_MESSAGE}", flush=True)
        return True
    return False


# ---------------------------------------------------------------------------
# 全局配置专属键（禁止写入 skill 级配置）
# ---------------------------------------------------------------------------

_GLOBAL_ONLY_KEYS = frozenset({"server_domain", "api_key", "api_secret", "user_token"})


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    """安全加载 YAML 文件，文件不存在或解析失败返回空 dict。"""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _merge_config(base: dict, override: dict) -> dict:
    """将 override 中的非空值合并到 base 中。"""
    for key, value in override.items():
        if value is not None and str(value).strip() != "":
            base[key] = value
    return base


def load_config() -> dict:
    """四层配置加载。

    加载优先级（高覆盖低）：
    1. default_config.yaml（包内默认值）
    2. ~/.qbi/config.yaml（QBI 全局配置）
    3. ~/.qbi/smartq-data-report/config.yaml（skill 级用户配置）
    4. ACCESS_TOKEN 环境变量（最高优先级）
    """
    # --- 第 1 层：包内默认配置 ---
    config = _load_yaml(DEFAULT_CONFIG_PATH)

    # --- 第 2 层：QBI 全局配置 ---
    global_config = _load_yaml(GLOBAL_CONFIG_PATH)
    _merge_config(config, global_config)

    # --- 第 3 层：skill 级用户配置 ---
    skill_config = _load_yaml(SKILL_CONFIG_PATH)
    _merge_config(config, skill_config)

    # --- 全局配置专属键清理 ---
    # server_domain / api_key / api_secret / user_token 应统一由全局配置管理。
    # 如果 skill 级配置中残留了这些键，自动清理并以全局配置为准。
    _resolve_global_key_conflicts(config, global_config, skill_config)

    # --- 第 4 层：环境变量覆盖（最高优先级） ---
    if config.get("use_env_property"):
        access_token = os.environ.get("ACCESS_TOKEN")
        if not access_token:
            raise ValueError("use_env_property 为 true 时，必须设置 ACCESS_TOKEN 环境变量")
        try:
            token_data = json.loads(access_token)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ACCESS_TOKEN 解析失败：{exc}") from exc

        env_mapping = {
            "qbi_api_key": "api_key",
            "qbi_api_secret": "api_secret",
            "qbi_server_domain": "server_domain",
            "qbi_user_token": "user_token",
        }
        for env_key, config_key in env_mapping.items():
            env_val = token_data.get(env_key)
            if env_val:
                config[config_key] = env_val

    # --- 试用凭证兜底 ---
    missing_key = not config.get("api_key")
    missing_secret = not config.get("api_secret")
    missing_token = not config.get("user_token")

    if missing_key and missing_secret and missing_token:
        _print_trial_welcome()

    if missing_key:
        config["api_key"] = _rv(_R0)
    if missing_secret:
        config["api_secret"] = _rv(_R1)

    return config


# ---------------------------------------------------------------------------
# 全局配置专属键冲突解决
# ---------------------------------------------------------------------------

def _resolve_global_key_conflicts(
    config: dict, global_config: dict, skill_config: dict
) -> None:
    """全局配置专属键应统一由全局配置管理，禁止残留在 skill 级配置中。

    对于每个全局专属键（server_domain / api_key / api_secret / user_token）：
    - 如果 skill 配置中存在该键，且全局配置中也有 → 以全局配置为准，移除 skill 残留
    - 如果 skill 配置中存在该键，但全局配置中没有 → 迁移到全局配置，再移除 skill 残留

    背景：
    - skill 级配置（优先级 3）> 全局配置（优先级 2）
    - 如果 skill 配置中残留全局专属键，会覆盖全局配置中的新值
    """
    keys_to_remove = []
    for key in _GLOBAL_ONLY_KEYS:
        skill_val = str(skill_config.get(key, "")).strip()
        if not skill_val:
            continue  # skill 配置中没有此键，无需处理

        global_val = str(global_config.get(key, "")).strip()
        if global_val:
            # 全局配置中已有此键 → 以全局值为准
            if skill_val != global_val:
                config[key] = global_val
                display_global = global_val[:8] + "..." if len(global_val) > 8 else global_val
                display_skill = skill_val[:8] + "..." if len(skill_val) > 8 else skill_val
                print(
                    f"[配置] skill 级配置中的 {key} ({display_skill}) "
                    f"与全局配置 ({display_global}) 不一致，以全局配置为准",
                    flush=True,
                )
        else:
            # 全局配置中没有此键 → 从 skill 迁移到全局
            persist_to_global_config(key, skill_val)
            print(
                f"[配置] 已将 {key} 从 skill 级配置迁移到全局配置 {GLOBAL_CONFIG_PATH}",
                flush=True,
            )
        keys_to_remove.append(key)

    # 批量清理 skill 配置中的全局专属键
    if keys_to_remove:
        _remove_keys_from_yaml(SKILL_CONFIG_PATH, keys_to_remove)


def _remove_keys_from_yaml(config_path: Path, keys: list) -> None:
    """从 YAML 配置文件中移除指定的键列表。"""
    if not config_path.exists():
        return
    try:
        key_prefixes = tuple(f"{k}:" for k in keys)
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = [line for line in lines if not line.lstrip().startswith(key_prefixes)]
        if len(new_lines) != len(lines):
            with open(config_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 服务域名获取
# ---------------------------------------------------------------------------


def get_server_domain(config: Optional[dict] = None) -> str:
    config = config or load_config()
    return str(config["server_domain"]).rstrip("/")


# ---------------------------------------------------------------------------
# 配置持久化
# ---------------------------------------------------------------------------

def persist_to_skill_config(key: str, value: str):
    """将单个配置项写入 skill 级用户配置文件。

    写入路径：~/.qbi/smartq-data-report/config.yaml

    注意：server_domain / api_key / api_secret / user_token 属于全局配置专属键，
    禁止写入 skill 级配置，请使用 persist_to_global_config()。
    """
    if key in _GLOBAL_ONLY_KEYS:
        raise ValueError(
            f"配置项 '{key}' 属于全局配置专属键，禁止写入 skill 级配置。"
            f"请使用 persist_to_global_config() 写入 {GLOBAL_CONFIG_PATH}"
        )
    _persist_to_yaml(
        SKILL_CONFIG_DIR,
        SKILL_CONFIG_PATH,
        key,
        value,
        header=(
            "# Quick BI 用户配置（此文件不受技能包更新影响）\n"
            "# 配置优先级：此文件 > ~/.qbi/config.yaml > 包内 default_config.yaml\n\n"
        ),
    )


def persist_to_global_config(key: str, value: str):
    """将单个配置项写入 QBI 全局配置文件。

    写入路径：~/.qbi/config.yaml
    """
    _persist_to_yaml(
        QBI_HOME,
        GLOBAL_CONFIG_PATH,
        key,
        value,
        header=(
            "# Quick BI 全局配置（所有 skill 共享，不受技能包更新影响）\n"
            "# 所有配置（server_domain、api_key、api_secret、user_token 等）建议放在此文件\n\n"
        ),
    )


def persist_to_default_config(key: str, value: str):
    """将单个配置项写入包内默认配置文件。

    写入路径：<skill>/default_config.yaml

    仅用于自动注册等场景，将自动生成的值（如 user_token）写回默认配置，
    避免污染用户的全局或 skill 级配置文件。
    """
    _persist_to_yaml(
        DEFAULT_CONFIG_PATH.parent,
        DEFAULT_CONFIG_PATH,
        key,
        value,
        header=(
            "# Quick BI 默认配置（随技能包发布）\n"
            "# 自动注册产生的 user_token 也会写入此处\n\n"
        ),
    )


def _persist_to_yaml(config_dir: Path, config_path: Path, key: str, value: str, header: str):
    """将单个键值对写入指定 YAML 配置文件。"""
    config_dir.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = [header]

    found = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{key}:"):
            lines[i] = f"{key}: {value}\n"
            found = True
            break

    if not found:
        lines.append(f"{key}: {value}\n")

    with open(config_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
