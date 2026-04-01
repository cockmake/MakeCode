# -*- coding: utf-8 -*-
"""
数据集解析模块：智能选表、用户权限查询、数据集相关性排序。

当用户未指定 cubeId 时，通过本模块自动匹配最合适的数据集。
"""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from utils import read_config, require_user_id, request_openapi


# ---------------------------------------------------------------------------
# 用户权限查询
# ---------------------------------------------------------------------------

def query_accessible_cubes(*, config: Optional[dict] = None) -> List[dict]:
    """
    调用 GET /openapi/v2/smartq/query/llmCubeWithThemeList 查询用户有权限的问数数据集。
    返回 [{"cubeId": "xxx", "cubeName": "yyy"}, ...] 列表。
    """
    config = config or read_config()
    user_id = require_user_id(config)

    resp = request_openapi(
        "GET",
        "/openapi/v2/smartq/query/llmCubeWithThemeList",
        params={"userId": user_id},
        config=config,
    )
    result = resp.json()

    if not result.get("success", False):
        raise RuntimeError(
            f"查询问数数据集权限失败: code={result.get('code')}, message={result.get('message')}"
        )

    data = result.get("data") or {}
    if isinstance(data, str):
        data = json.loads(data)
    cube_ids_map = (data.get("cubeIds") if isinstance(data, dict) else {}) or {}

    return [
        {"cubeId": cid, "cubeName": cname}
        for cid, cname in cube_ids_map.items()
    ]


# ---------------------------------------------------------------------------
# 数据集相关性排序
# ---------------------------------------------------------------------------

def rank_cubes_by_relevance(
    question: str, cubes: List[dict], top_n: int = 2
) -> List[dict]:
    """
    根据用户问题与数据集名称的文本相关性对数据集排序，返回最相关的 top_n 个。

    评分策略：
    1. cubeName 是 question 的子串 → 高分加成
    2. question 中包含 cubeName 的连续子串 → 按最长匹配长度加分
    3. 共同字符占 cubeName 长度的比例 → 基础分
    """
    scored: List[tuple] = []
    q_lower = question.lower()

    for cube in cubes:
        name = cube.get("cubeName", "")
        if not name:
            scored.append((0.0, cube))
            continue

        n_lower = name.lower()
        score = 0.0

        if n_lower in q_lower:
            score += 100.0
        elif q_lower in n_lower:
            score += 80.0

        matcher = SequenceMatcher(None, q_lower, n_lower)
        longest = matcher.find_longest_match(0, len(q_lower), 0, len(n_lower))
        if longest.size > 0:
            score += (longest.size / max(len(n_lower), 1)) * 50.0

        common_chars = set(q_lower) & set(n_lower)
        name_chars = set(n_lower)
        if name_chars:
            score += (len(common_chars) / len(name_chars)) * 30.0

        scored.append((score, cube))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored[:top_n]]


# ---------------------------------------------------------------------------
# 智能选表
# ---------------------------------------------------------------------------

def call_table_search(
    question: str,
    *,
    cube_ids: Optional[List[str]] = None,
    config: Optional[dict] = None,
) -> List[str]:
    """
    调用 POST /openapi/v2/smartq/tableSearch 进行智能选表。
    当用户未指定 cubeId 时，根据问题自动匹配最合适的数据集。
    返回匹配到的 cubeId 列表。
    """
    config = config or read_config()
    user_id = require_user_id(config)

    payload: Dict[str, Any] = {
        "userId": user_id,
        "userQuestion": question,
        "llmNameForInference": "SYSTEM_deepseek-r1-0528",
    }
    if cube_ids:
        payload["cubeIds"] = cube_ids

    resp = request_openapi(
        "POST",
        "/openapi/v2/smartq/tableSearch",
        json_body=payload,
        config=config,
    )
    body = resp.json()
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        if str(body.get("success", "")).lower() != "true":
            raise RuntimeError(f"tableSearch 失败: [{body.get('code')}] {body.get('message')}")
        data = body.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, str) and data != "null":
            try:
                parsed = json.loads(data)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
    return []


# ---------------------------------------------------------------------------
# 组合：数据集解析
# ---------------------------------------------------------------------------

def resolve_cube_id(
    question: str,
    *,
    cube_ids: Optional[List[str]] = None,
    config: Optional[dict] = None,
) -> Optional[str]:
    """
    完整的数据集解析流程：
    1. 查询用户有权限的问数数据集列表
    2. 将权限数据集 cubeIds（与调用方传入的候选合并）作为参数，调用智能选表
    3. 智能选表未匹配时，按文本相关性从权限数据集中选择最相关的

    返回解析到的 cubeId，全部失败时返回 None。
    """
    config = config or read_config()

    print(f"{'=' * 60}", flush=True)
    print(f"[智能选表] 未指定数据集，正在根据问题自动匹配 ...", flush=True)
    print(f"[智能选表] 问题: {question}", flush=True)
    print(f"{'=' * 60}", flush=True)

    # Step 1: 查询用户有权限的数据集
    try:
        accessible = query_accessible_cubes(config=config)
    except Exception as e:
        print(f"[权限查询失败] GET /openapi/v2/smartq/query/llmCubeWithThemeList 调用异常:\n  {e}", flush=True)
        return None

    if not accessible:
        print("[权限查询] 该用户没有任何数据集的问数权限", flush=True)
        print(
            "\n============================================================\n"
            "您当前没有可用的问数数据集。\n\n"
            "📂 试试「文件问数」\n"
            "无需任何权限配置，上传 Excel/CSV 文件即可直接分析。\n\n"
            "🚀 0 元体验，限时加码\n"
            "现在上阿里云，将额外赠送 30 天全功能体验，解锁企业级安全管控与深度分析引擎，\n"
            "让 AI 洞察更准、更稳。点击下方链接，领取试用：\n"
            "https://www.aliyun.com/product/quickbi-smart?utm_content=g_1000411205\n\n"
            "💬 点击下方链接，进入交流群获取最新资讯：\n"
            "https://at.umtrack.com/r4Tnme\n"
            "============================================================",
            flush=True,
        )
        return None

    print(f"[权限查询] 用户共有 {len(accessible)} 个可用数据集:", flush=True)
    for item in accessible[:10]:
        print(f"  - {item['cubeId']}  {item['cubeName']}", flush=True)
    if len(accessible) > 10:
        print(f"  ... 共 {len(accessible)} 个", flush=True)

    # Step 2: 合并权限 cubeIds 与调用方传入的候选
    accessible_ids = [item["cubeId"] for item in accessible]
    if cube_ids:
        merged = list(dict.fromkeys(cube_ids + accessible_ids))
    else:
        merged = accessible_ids

    # Step 3: 带 cubeIds 调用智能选表
    matched_cube_ids: List[str] = []
    try:
        matched_cube_ids = call_table_search(question, cube_ids=merged, config=config)
    except Exception as e2:
        print(f"[智能选表失败] POST /openapi/v2/smartq/tableSearch 调用异常:\n  {e2}", flush=True)

    if matched_cube_ids:
        cube_id = matched_cube_ids[0]
        print(f"[智能选表] 匹配到数据集: {cube_id}", flush=True)
        if len(matched_cube_ids) > 1:
            print(f"[智能选表] 其他候选: {matched_cube_ids[1:]}", flush=True)
        return cube_id

    # Step 4: 智能选表未匹配，按文本相关性从权限数据集中选择
    ranked = rank_cubes_by_relevance(question, accessible, top_n=2)
    cube_id = ranked[0]["cubeId"]
    print(f"[相关性匹配] 智能选表未返回结果，根据问题与数据集名称相关性选择:", flush=True)
    for i, rc in enumerate(ranked):
        tag = "→ 选定" if i == 0 else "  候选"
        print(f"  {tag}: {rc['cubeId']}  {rc['cubeName']}", flush=True)
    return cube_id
