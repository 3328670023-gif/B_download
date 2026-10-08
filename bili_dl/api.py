"""B 站 Web API 封装：视频信息、播放地址、收藏夹、投稿、合集、字幕。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .session import API_HOST, BiliError, HttpClient
from .utils import debug, warn
from .wbi import build_mixin_key, cache_mixin_key, cached_mixin_key

# 视频清晰度（qn -> 名称）
QUALITY_NAMES: Dict[int, str] = {
    127: "8K 超高清",
    126: "杜比视界",
    125: "HDR 真彩",
    120: "4K 超清",
    116: "1080P60 高帧率",
    112: "1080P+ 高码率",
    100: "智能修复",
    80: "1080P 高清",
    74: "720P60 高帧率",
    64: "720P 准高清",
    32: "480P 标清",
    16: "360P 流畅",
    6: "240P 极速",
}

AUDIO_QUALITY_NAMES: Dict[int, str] = {
    30216: "64K",
    30232: "132K",
    30280: "192K",
    30250: "杜比全景声",
    30251: "Hi-Res 无损",
}

# 命令行里可以用的清晰度别名
QUALITY_ALIASES: Dict[str, int] = {
    "8k": 127, "4k": 120, "1080p60": 116, "1080p+": 112, "1080p": 80,
    "720p60": 74, "720p": 64, "480p": 32, "360p": 16, "240p": 6,
    "best": 127, "highest": 127, "max": 127,
}


def quality_label(qn: int) -> str:
    return QUALITY_NAMES.get(qn, f"qn{qn}")


@dataclass
class Page:
    """视频分 P。"""
    index: int
    cid: int
    title: str
    duration: int = 0
    ep_id: int = 0


@dataclass
class VideoInfo:
    """一个可下载的视频条目。"""
    bvid: str
    aid: int
    title: str
    pages: List[Page] = field(default_factory=list)
    owner: str = ""
    owner_mid: int = 0
    cover: str = ""
    desc: str = ""
    duration: int = 0
    pubdate: int = 0
    # 番剧/影视特有
    ep_id: int = 0
    season_id: int = 0
    is_pgc: bool = False

    @property
    def single_page(self) -> bool:
        return len(self.pages) == 1

    def page_by_index(self, index: int) -> Optional[Page]:
        for p in self.pages:
            if p.index == index:
                return p
        return None


@dataclass
class Stream:
    """一路可下载的媒体流。"""
    kind: str            # video / audio
    urls: List[str]      # baseUrl + backupUrl
    size: int = 0
    quality: int = 0
    codecs: str = ""
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    suffix: str = "m4s"

    @property
    def label(self) -> str:
        if self.kind == "video":
            name = quality_label(self.quality)
            res = f" {self.width}x{self.height}" if self.width else ""
            return f"{name}{res} {self.codecs}"
        return f"音频 {AUDIO_QUALITY_NAMES.get(self.quality, self.quality)} {self.codecs}"


@dataclass
class Subtitle:
    lan: str
    lan_doc: str
    url: str


class BiliAPI:
    """对 api.bilibili.com 的高层封装。"""

    def __init__(self, client: HttpClient):
        self.client = client

    # ------------------------------------------------------------ WBI ---
    def mixin_key(self, refresh: bool = False) -> str:
        """获取（并缓存）wbi mixin key。"""
        if not refresh and cached_mixin_key():
            return cached_mixin_key()
        try:
            data = self.client.api("/x/web-interface/nav", retries=2)
        except BiliError:
            data = {}
        wbi = (data or {}).get("wbi_img") or {}
        img, sub = wbi.get("img_url", ""), wbi.get("sub_url", "")
        if not img or not sub:
            # 兜底：直接从接口原始响应取（nav 未登录也可能带 wbi_img）
            raw = self.client.get_json(f"{API_HOST}/x/web-interface/nav")
            wbi = ((raw.get("data") or {}).get("wbi_img")) or {}
            img, sub = wbi.get("img_url", ""), wbi.get("sub_url", "")
        if not img or not sub:
            debug("未能取得 wbi 密钥，将使用未签名接口")
            return ""
        key = build_mixin_key(img, sub)
        cache_mixin_key(key)
        return key

    def is_login(self) -> bool:
        """是否已登录（结果缓存，避免重复请求）。"""
        cached = getattr(self, "_login_cache", None)
        if cached is not None:
            return cached
        try:
            d = self.client.api("/x/web-interface/nav", retries=2)
            self._login_cache = bool(d.get("isLogin"))
        except Exception:
            self._login_cache = False
        return self._login_cache

    def self_info(self) -> Dict[str, Any]:
        try:
            return self.client.api("/x/web-interface/nav", retries=2)
        except Exception:
            return {}

    # ------------------------------------------------------- 视频信息 ---
    def video_info(self, bvid: str = "", aid: int = 0) -> VideoInfo:
        params: Dict[str, Any] = {}
        if bvid:
            params["bvid"] = bvid
        elif aid:
            params["aid"] = aid
        else:
            raise ValueError("必须提供 bvid 或 aid")
        d = self.client.api("/x/web-interface/view", params)
        pages = []
        for p in d.get("pages") or []:
            pages.append(Page(index=int(p.get("page", 1)), cid=int(p["cid"]),
                              title=(p.get("part") or "").strip(),
                              duration=int(p.get("duration") or 0)))
        cid0 = int(d.get("cid") or 0)
        if not pages:
            pages = [Page(index=1, cid=cid0, title=(d.get("title") or "").strip(),
                          duration=int(d.get("duration") or 0))]
        owner = d.get("owner") or {}
        return VideoInfo(
            bvid=d.get("bvid", bvid),
            aid=int(d.get("aid") or aid),
            title=(d.get("title") or "").strip(),
            pages=pages,
            owner=owner.get("name", ""),
            owner_mid=int(owner.get("mid") or 0),
            cover=d.get("pic", ""),
            desc=(d.get("desc") or "").strip(),
            duration=int(d.get("duration") or 0),
            pubdate=int(d.get("pubdate") or 0),
        )

    # ------------------------------------------------------- 播放地址 ---
    def _playurl_params(self, cid: int, qn: int, fnval: int, bvid: str = "",
                        avid: int = 0) -> Dict[str, Any]:
        p: Dict[str, Any] = {
            "cid": cid,
            "qn": qn,
            "fnver": 0,
            "fnval": fnval,
            "fourk": 1,
        }
        if bvid:
            p["bvid"] = bvid
        elif avid:
            p["avid"] = avid
        return p

    def playurl(self, cid: int, qn: int = 127, fnval: int = 4048, bvid: str = "",
                avid: int = 0) -> Dict[str, Any]:
        """获取播放地址（DASH 或 progressive）。"""
        params = self._playurl_params(cid, qn, fnval, bvid, avid)
        key = self.mixin_key()
        errors = []
        if key:
            try:
                return self.client.api("/x/player/wbi/playurl", params, wbi=key)
            except BiliError as exc:
                errors.append(exc)
                if exc.code in (-403, -352):  # 签名过期，刷新后重试一次
                    key = self.mixin_key(refresh=True)
                    if key:
                        try:
                            return self.client.api("/x/player/wbi/playurl", params, wbi=key)
                        except BiliError as exc2:
                            errors.append(exc2)
        try:
            return self.client.api("/x/player/playurl", params)
        except BiliError as exc:
            errors.append(exc)
            raise errors[0]

    def pgc_playurl(self, ep_id: int, cid: int, qn: int = 127,
                    fnval: int = 4048, avid: int = 0) -> Dict[str, Any]:
        """番剧/影视的播放地址（返回体在 result 字段）。"""
        params = self._playurl_params(cid, qn, fnval, avid=avid)
        params["ep_id"] = ep_id
        key = self.mixin_key()
        url = "/pgc/player/web/playurl"
        if key:
            from .wbi import sign_url
            full = sign_url(API_HOST + url, params, key)
        else:
            import urllib.parse
            full = f"{API_HOST}{url}?{urllib.parse.urlencode(params)}"
        data = self.client.get_json(full)
        if data.get("code") != 0:
            raise BiliError(int(data.get("code", -1)), str(data.get("message", "")), full)
        return data.get("result") or data.get("data") or {}

    # ------------------------------------------------------ 番剧信息 ---
    def _pgc_request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """PGC 系列接口的返回体在 result 字段（不是 data）。"""
        import urllib.parse
        full = f"{API_HOST}{path}?{urllib.parse.urlencode(params)}"
        raw = self.client.get_json(full, retries=2)
        if raw.get("code") != 0:
            raise BiliError(int(raw.get("code", -1)), str(raw.get("message", "")), full)
        return raw.get("result") or raw.get("data") or {}

    def pgc_season(self, ep_id: int = 0, season_id: int = 0) -> VideoInfo:
        params: Dict[str, Any] = {}
        if ep_id:
            params["ep_id"] = ep_id
        elif season_id:
            params["season_id"] = season_id
        d = self._pgc_request("/pgc/view/web/season", params)
        eps = d.get("episodes") or []
        pages = []
        # 番剧的 "cid" 在每个 episode 上
        for i, ep in enumerate(eps, start=1):
            num = str(ep.get("title") or i).strip()
            name = (ep.get("long_title") or ep.get("show_title") or "").strip()
            pages.append(Page(index=i, cid=int(ep.get("cid") or 0),
                              title=(f"第{num}话 {name}".strip() if name and num.isdigit()
                                     else (name or num or f"第{i}话")),
                              duration=int(ep.get("duration") or 0) // 1000,
                              ep_id=int(ep.get("id") or 0)))
        info = VideoInfo(
            bvid="",
            aid=0,
            title=(d.get("season_title") or d.get("title") or "").strip(),
            pages=pages,
            owner=((d.get("up_info") or {}).get("uname") or ""),
            cover=d.get("cover", "") or d.get("square_cover", ""),
            desc=(d.get("evaluate") or "").strip(),
            ep_id=ep_id,
            season_id=int(d.get("season_id") or season_id),
            is_pgc=True,
        )
        return info

    def pgc_episode(self, ep_id: int) -> Dict[str, Any]:
        d = self._pgc_request("/pgc/view/web/season", {"ep_id": ep_id})
        for ep in d.get("episodes") or []:
            if int(ep.get("id") or 0) == ep_id:
                return ep
        eps = d.get("episodes") or []
        return eps[0] if eps else {}

    # -------------------------------------------------------- 收藏夹 ---
    def fav_list(self, media_id: int, max_items: int = 0):
        """遍历收藏夹，逐条 yield (bvid, title)。"""
        pn = 1
        total = None
        yielded = 0
        while True:
            d = self.client.api("/x/v3/fav/resource/list", {
                "media_id": media_id, "pn": pn, "ps": 20, "platform": "web",
                "order": "mtime", "type": 0, "tid": 0,
            })
            info = d.get("info") or {}
            medias = d.get("medias") or []
            if total is None:
                total = int(info.get("media_count") or 0)
            if not medias:
                break
            for m in medias:
                if int(m.get("type") or 0) != 2:  # 2 = 视频
                    continue
                bvid = m.get("bvid") or ""
                if not bvid:
                    continue
                yield bvid, (m.get("title") or "").strip()
                yielded += 1
                if max_items and yielded >= max_items:
                    return
            if not d.get("has_more"):
                break
            pn += 1

    # -------------------------------------------------- UP 主投稿列表 ---
    def space_videos(self, mid: int, max_items: int = 0, order: str = "pubdate",
                     keyword: str = ""):
        """遍历 UP 主投稿，逐条 yield (bvid, title)。"""
        self.client.ensure_warm()
        pn = 1
        yielded = 0
        while True:
            params: Dict[str, Any] = {
                "mid": mid, "ps": 30, "pn": pn, "order": order,
                "tid": 0, "platform": "web", "web_location": 1550101,
            }
            if keyword:
                params["keyword"] = keyword
            key = self.mixin_key()
            try:
                d = self.client.api("/x/space/wbi/arc/search", params, wbi=key, retries=2)
            except BiliError as exc:
                if exc.code not in (-352, -412):
                    raise
                # 命中风控：刷新指纹与签名后再试一次，仍失败则给出可操作的提示
                self.client.warm_up()
                try:
                    d = self.client.api("/x/space/wbi/arc/search", params,
                                        wbi=self.mixin_key(refresh=True))
                except BiliError as exc2:
                    raise BiliError(
                        exc2.code,
                        "B 站风控校验失败，该接口需要登录态。请用 "
                        "--cookie \"SESSDATA=...\" 传入浏览器 Cookie（见 README 第六节），"
                        "或稍后降低频率重试",
                        exc2.url) from exc2
            vlist = ((d.get("list") or {}).get("vlist")) or []
            if not vlist:
                break
            for v in vlist:
                bvid = v.get("bvid") or ""
                if not bvid:
                    continue
                yield bvid, (v.get("title") or "").strip()
                yielded += 1
                if max_items and yielded >= max_items:
                    return
            page = d.get("page") or {}
            if pn * 30 >= int(page.get("count") or 0):
                break
            pn += 1

    # ------------------------------------------------------ 合集/列表 ---
    def season_archives(self, mid: int, season_id: int, max_items: int = 0):
        """遍历合集（视频列表），逐条 yield (bvid, title)。"""
        self.client.ensure_warm()
        page = 1
        yielded = 0
        while True:
            d = self.client.api("/x/polymer/web-space/seasons_archives_list", {
                "mid": mid, "season_id": season_id,
                "sort_reverse": "false", "page_num": page, "page_size": 30,
            }, retries=2)
            archives = d.get("archives") or []
            if not archives:
                break
            for a in archives:
                bvid = a.get("bvid") or ""
                if not bvid:
                    continue
                yield bvid, (a.get("title") or "").strip()
                yielded += 1
                if max_items and yielded >= max_items:
                    return
            if not d.get("page", {}).get("total") or len(archives) < 30:
                break
            page += 1

    # --------------------------------------------------------- 搜索 ---
    def search(self, keyword: str, max_items: int = 0, page_limit: int = 0):
        """关键字搜索。

        注意：B 站的搜索接口经常随机返回空结果（风控/AB 实验），
        因此这里对首页做了多次重试，并在 wbi 接口失效时回退到旧接口。
        """
        self.client.ensure_warm()
        page = 1
        yielded = 0
        while True:
            results = []
            for attempt in range(1, 4):
                try:
                    d = self.client.api("/x/web-interface/wbi/search/type", {
                        "search_type": "video", "keyword": keyword, "page": page,
                        "page_size": 42, "order": "totalrank",
                    }, wbi=self.mixin_key(), retries=2)
                    results = d.get("result") or []
                except BiliError as exc:
                    debug(f"搜索失败（第 {attempt} 次）：{exc}")
                    results = []
                if results:
                    break
                time.sleep(0.8 * attempt)
            if not results and page == 1:
                try:  # 旧接口兜底
                    d = self.client.api("/x/web-interface/search/type", {
                        "search_type": "video", "keyword": keyword, "page": page,
                        "page_size": 42,
                    }, retries=2)
                    results = d.get("result") or []
                except Exception as exc:
                    debug(f"旧搜索接口也失败：{exc}")
            if not results:
                break
            for r in results:
                bvid = r.get("bvid") or ""
                if not bvid:
                    continue
                title = re.sub(r"<.*?>", "", r.get("title") or "")
                yield bvid, title
                yielded += 1
                if max_items and yielded >= max_items:
                    return
            if page_limit and page >= page_limit:
                break
            page += 1
            if page > 50:
                break

    # --------------------------------------------------------- 字幕 ---
    def subtitle_list(self, bvid: str, cid: int, aid: int = 0) -> List[Subtitle]:
        """获取 CC 字幕列表（未登录通常为空）。"""
        params: Dict[str, Any] = {"cid": cid}
        if bvid:
            params["bvid"] = bvid
        elif aid:
            params["aid"] = aid
        try:
            d = self.client.api("/x/player/wbi/v2", params, wbi=self.mixin_key(), retries=2)
        except Exception as exc:
            debug(f"取字幕列表失败：{exc}")
            return []
        subs = ((d.get("subtitle") or {}).get("subtitles")) or []
        out = []
        for s in subs:
            url = s.get("subtitle_url") or ""
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = "https://" + url.lstrip("/")
            if url:
                out.append(Subtitle(lan=s.get("lan", ""),
                                    lan_doc=s.get("lan_doc", ""), url=url))
        return out
