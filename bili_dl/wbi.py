"""B 站 WBI 签名算法实现。

B 站从 2023 年起对部分 Web 接口（如 space/arc/search、player/wbi/playurl）
启用了 wbi 签名校验：把请求参数按 key 排序后拼成 query，再拼接由
img_key/sub_key 重排得到的 mixin_key，取 md5 作为 w_rid 参数。

参考实现逻辑（公开逆向结果）：
    mixin_key = reorder(img_key + sub_key, MIXIN_KEY_ENC_TAB)[:32]
    w_rid     = md5(sorted_query + mixin_key)
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from typing import Dict, Mapping

# 用于打乱 (img_key + sub_key) 的下标表，共 64 项
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# WBI 要求剔除这些字符
_FILTER_CHARS = re.compile(r"[!'()*]")

_MIXIN_KEY_CACHE: Dict[str, str] = {}


def get_mixin_key(orig: str) -> str:
    """按固定下标表重排字符串，取前 32 位作为 mixin_key。"""
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def key_from_url(url: str) -> str:
    """从 https://i0.hdslb.com/bfs/wbi/xxxx.png 中取出 xxxx。"""
    if not url:
        return ""
    return url.rsplit("/", 1)[-1].split(".")[0]


def build_mixin_key(img_url: str, sub_url: str) -> str:
    raw = key_from_url(img_url) + key_from_url(sub_url)
    if len(raw) < 64:
        raise ValueError("wbi 密钥长度不足，可能接口返回异常")
    return get_mixin_key(raw)


def cache_mixin_key(key: str) -> None:
    _MIXIN_KEY_CACHE["key"] = key


def cached_mixin_key() -> str:
    return _MIXIN_KEY_CACHE.get("key", "")


def enc_wbi(params: Mapping[str, object], mixin_key: str,
            wts: int | None = None) -> Dict[str, object]:
    """对参数做 wbi 签名，返回带 wts 与 w_rid 的新字典。"""
    signed = {k: v for k, v in params.items() if v is not None}
    signed["wts"] = int(wts if wts is not None else time.time())
    signed = dict(sorted(signed.items(), key=lambda kv: kv[0]))
    # 值里的 !'()* 必须先剔除，否则签名对不上
    signed = {k: _FILTER_CHARS.sub("", str(v)) for k, v in signed.items()}
    query = urllib.parse.urlencode(signed)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed


def sign_url(url: str, params: Mapping[str, object], mixin_key: str) -> str:
    """把签名后的参数拼到 URL 上。"""
    signed = enc_wbi(params, mixin_key)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urllib.parse.urlencode(signed)}"
