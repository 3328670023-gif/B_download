"""输入解析：把各种 B 站链接/ID 转成统一的目标描述。"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

from .session import HttpClient
from .utils import debug

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"av(\d+)", re.I)
EP_RE = re.compile(r"ep(\d+)", re.I)
SS_RE = re.compile(r"ss(\d+)", re.I)
SPACE_RE = re.compile(r"space\.bilibili\.com/(\d+)")
SEASON_RE = re.compile(r"space\.bilibili\.com/(\d+)/channel/(?:collectiondetail|seriesdetail)\?[^#]*\bsid=(\d+)")
FAVLIST_RE = re.compile(r"space\.bilibili\.com/(\d+)/favlist\?[^#]*\bfid=(\d+)")
MEDIALIST_RE = re.compile(r"(?:medialist/detail/)?ml(\d+)")
LIST_SID_RE = re.compile(r"bilibili\.com/list/(\d+)\?[^#]*\bsid=(\d+)")
PLAYLIST_RE = re.compile(r"bilibili\.com/list/(\d+)")
SPACE_VIDEO_RE = re.compile(r"space\.bilibili\.com/(\d+)(?:/video|/\?|/?$|/dynamic)")

KIND_VIDEO = "video"
KIND_BANGUMI = "bangumi"
KIND_SPACE = "space"
KIND_SEASON = "season"
KIND_FAV = "fav"
KIND_SEARCH = "search"


@dataclass
class Target:
    """一次解析得到的目标资源。"""
    kind: str
    raw: str
    bvid: str = ""
    aid: int = 0
    ep_id: int = 0
    season_id: int = 0
    mid: int = 0
    media_id: int = 0
    page: int = 0
    keyword: str = ""

    def describe(self) -> str:
        if self.kind == KIND_VIDEO:
            base = self.bvid or f"av{self.aid}"
            return f"视频 {base}" + (f" 第{self.page}P" if self.page else "")
        if self.kind == KIND_BANGUMI:
            return f"番剧 ep{self.ep_id}" if self.ep_id else f"番剧 ss{self.season_id}"
        if self.kind == KIND_SPACE:
            return f"UP主 {self.mid} 的投稿"
        if self.kind == KIND_SEASON:
            return f"合集/列表 sid={self.season_id} (UP {self.mid})"
        if self.kind == KIND_FAV:
            return f"收藏夹 media_id={self.media_id}"
        if self.kind == KIND_SEARCH:
            return f"搜索关键字「{self.keyword}」"
        return self.raw


def resolve_short_link(client: HttpClient, url: str) -> str:
    """展开 b23.tv 短链。"""
    try:
        req = client.build_request(url)
        with client.opener.open(req, timeout=15) as resp:
            return resp.geturl()
    except Exception as exc:  # 短链偶发超时，尽力而为
        debug(f"短链解析失败 {url}: {exc}")
        return url


def parse_target(text: str, client: Optional[HttpClient] = None) -> Target:
    """解析单个输入为 Target；无法识别时当作搜索关键字。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("空输入")
    url = raw
    if "b23.tv" in url or "bili2233.cn" in url:
        if client is not None:
            url = resolve_short_link(client, url if url.startswith("http") else "https://" + url)

    # 番剧
    m = EP_RE.search(url)
    if m and ("bangumi" in url or "ep" in url.split("?")[0][-4:] or "play" in url):
        return Target(kind=KIND_BANGUMI, raw=raw, ep_id=int(m.group(1)))
    m = SS_RE.search(url)
    if m and "bangumi" in url:
        return Target(kind=KIND_BANGUMI, raw=raw, season_id=int(m.group(1)))

    # 收藏夹
    m = FAVLIST_RE.search(url)
    if m:
        return Target(kind=KIND_FAV, raw=raw, mid=int(m.group(1)), media_id=int(m.group(2)))
    m = MEDIALIST_RE.search(url)
    if m and "bilibili.com" in url and ("medialist" in url or "list" in url):
        return Target(kind=KIND_FAV, raw=raw, media_id=int(m.group(1)))

    # 合集 / 视频列表
    m = SEASON_RE.search(url)
    if m:
        return Target(kind=KIND_SEASON, raw=raw, mid=int(m.group(1)), season_id=int(m.group(2)))
    m = LIST_SID_RE.search(url)
    if m:
        return Target(kind=KIND_SEASON, raw=raw, mid=int(m.group(1)), season_id=int(m.group(2)))

    # 普通视频 / 分 P
    page = 0
    mpage = re.search(r"[?&]p=(\d+)", url)
    if mpage:
        page = int(mpage.group(1))
    m = BV_RE.search(url)
    if m:
        return Target(kind=KIND_VIDEO, raw=raw, bvid=m.group(1), page=page)
    m = AV_RE.search(url)
    if m and ("bilibili.com" in url or url.lower().startswith("av")):
        return Target(kind=KIND_VIDEO, raw=raw, aid=int(m.group(1)), page=page)

    # UP 主空间
    m = SPACE_RE.search(url)
    if m:
        return Target(kind=KIND_SPACE, raw=raw, mid=int(m.group(1)))

    if raw.isdigit() and len(raw) >= 5:
        # 纯数字：优先当 UP 主 mid 处理需要用户显式指定，这里给提示性报错
        raise ValueError(
            f"无法确定「{raw}」的含义：纯数字既可能是 av 号也可能是 UP 主 mid。\n"
            f"  下载视频请用 av{raw} 或完整链接；下载 UP 主投稿请用 space.bilibili.com/{raw}"
        )

    return Target(kind=KIND_SEARCH, raw=raw, keyword=raw)


def looks_like_target(text: str) -> bool:
    """粗略判断是否是链接/ID（否则当搜索词）。"""
    if "bilibili.com" in text or "b23.tv" in text:
        return True
    if BV_RE.fullmatch(text.strip()) or AV_RE.fullmatch(text.strip()):
        return True
    return False
