"""统一 Steam 查询指令的输入分类。"""

import re


def search_mode(content: str) -> str:
    """返回 name、short_numeric 或 id；仅 ASCII 纯数字属于 AppID 候选。"""
    if not re.fullmatch(r"[0-9]+", content):
        return "name"
    return "short_numeric" if len(content) < 4 else "id"


def is_missing_appid_error(error: str | None) -> bool:
    """只将 Steam 明确的 AppID 空结果视为补搜条件；网络和限流错误不算。"""
    return bool(error and (
        error.startswith("接口未返回 AppID ")
        or ("AppID " in error and "不存在或在当前地区不可见" in error)
    ))
