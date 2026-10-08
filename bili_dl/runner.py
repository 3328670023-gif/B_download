"""下载调度层。

把「一个目标字符串」变成实际的下载动作。CLI 与 Web 界面共用这里的逻辑，
保证两边功能完全一致（单视频 / 分P / 番剧 / 收藏夹 / UP主投稿 / 合集 / 搜索）。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .api import BiliAPI, VideoInfo, quality_label
from .parser import (KIND_BANGUMI, KIND_FAV, KIND_SEARCH, KIND_SEASON,
                     KIND_SPACE, KIND_VIDEO, Target, parse_target)
from .session import BiliError, HttpClient, RiskControlError
from .utils import bold, debug, dim, error, info, warn


# ------------------------------------------------------------ 目标预解析 ---

def preview_target(target_text: str, api: BiliAPI, client: HttpClient,
                   limit: int = 50, with_context: bool = False):
    """解析目标但不下载，返回给界面展示的结构化信息。

    with_context=True 时返回 (result, ctx)，ctx 里带着 VideoInfo/Page 对象，
    方便调用方继续查询可用画质。
    """
    target = parse_target(target_text, client)
    result: Dict[str, Any] = {
        "input": target_text,
        "kind": target.kind,
        "describe": target.describe(),
    }
    ctx: Dict[str, Any] = {}
    done = None

    if target.kind == KIND_VIDEO:
        video = api.video_info(bvid=target.bvid, aid=target.aid)
        page = video.page_by_index(target.page) if target.page else None
        if target.page and page is None:
            page = video.pages[0]
        ctx["video"], ctx["page"] = video, page or video.pages[0]
        _fill_video(result, video, target.page)
        done = result

    elif target.kind == KIND_BANGUMI:
        if target.season_id:
            video = api.pgc_season(season_id=target.season_id)
            ctx["video"], ctx["page"] = video, video.pages[0] if video.pages else None
            _fill_video(result, video, 0)
            done = result
        else:
            season = api.pgc_season(ep_id=target.ep_id)
            page = next((p for p in season.pages if p.ep_id == target.ep_id), None)
            if page is None:
                ctx["video"], ctx["page"] = season, season.pages[0] if season.pages else None
                _fill_video(result, season, 0)
            else:
                single = season
                single.pages = [page]
                single.title = f"{season.title} - {page.title}".strip(" -")
                single.ep_id = target.ep_id
                ctx["video"], ctx["page"] = single, page
                _fill_video(result, single, 1)
            done = result

    if done is not None:
        if with_context:
            return done, ctx
        return done

    source = {
        KIND_FAV: ("收藏夹", f"收藏夹 {target.media_id}"),
        KIND_SPACE: ("UP主投稿", f"UP主 {target.mid} 的投稿"),
        KIND_SEASON: ("合集", f"合集 {target.season_id}"),
        KIND_SEARCH: ("搜索结果", f"搜索「{target.keyword}」"),
    }
    if target.kind in source:
        label, title = source[target.kind]
        if target.kind == KIND_FAV:
            items = list(api.fav_list(target.media_id, limit))
        elif target.kind == KIND_SPACE:
            items = list(api.space_videos(target.mid, limit))
        elif target.kind == KIND_SEASON:
            items = list(api.season_archives(target.mid, target.season_id, limit))
        else:
            items = list(api.search(target.keyword, limit))
        result.update(kind="collection", title=title, source=label,
                      items=_items(items), total=len(items),
                      preview_limited=bool(limit and len(items) >= limit))
        if with_context:
            return result, ctx
        return result

    raise ValueError(f"无法识别的目标：{target_text}")


def _items(pairs: List[Tuple[str, str]]) -> List[Dict[str, str]]:
    return [{"bvid": b, "title": t} for b, t in pairs]


def _fill_video(result: Dict[str, Any], video: VideoInfo, page_no: int) -> None:
    page = video.page_by_index(page_no) if page_no else None
    result.update(
        kind="video",
        title=video.title,
        owner=video.owner,
        cover=video.cover,
        desc=(video.desc or "")[:400],
        duration=page.duration if page else video.duration,
        page_count=len(video.pages),
        pages=[{"index": p.index, "title": p.title, "duration": p.duration,
                "cid": p.cid} for p in video.pages[:200]],
        bvid=video.bvid,
        url=(f"https://www.bilibili.com/video/{video.bvid}"
             + (f"?p={page_no}" if page_no and len(video.pages) > 1 else ""))
        if video.bvid else f"https://www.bilibili.com/bangumi/play/ep{video.ep_id}",
        single_page_index=page_no or 0,
    )


# -------------------------------------------------------------- 实际下载 ---

def run_target(target_text: str, api: BiliAPI, dl, client: HttpClient) -> None:
    """解析并下载一个目标（与 CLI 行为一致）。"""
    target = parse_target(target_text, client)
    opts = dl.opts

    if target.kind == KIND_VIDEO:
        video = api.video_info(bvid=target.bvid, aid=target.aid)
        if target.page:
            page = video.page_by_index(target.page)
            if page is None:
                warn(f"该视频没有第 {target.page} P，将下载全部分P")
            elif len(video.pages) > 1:
                video.pages = [page]
        dl.download_video(video, opts.pages)
        return

    if target.kind == KIND_BANGUMI:
        if target.season_id:
            video = api.pgc_season(season_id=target.season_id)
            dl.download_video(video, opts.pages)
            return
        season = api.pgc_season(ep_id=target.ep_id)
        page = next((p for p in season.pages if p.ep_id == target.ep_id), None)
        if page is None:
            warn("未找到该集，改为下载整季")
            dl.download_video(season, opts.pages)
            return
        single = season
        single.pages = [page]
        single.title = f"{season.title} - {page.title}".strip(" -")
        single.ep_id = target.ep_id
        dl.download_video(single, None)
        return

    if target.kind == KIND_FAV:
        info("正在读取收藏夹…")
        items = list(api.fav_list(target.media_id, opts.max_items))
        if not items:
            warn("收藏夹为空，或需要登录（请配置 SESSDATA Cookie）")
            return
        download_batch(items, api, dl, f"收藏夹_{target.media_id}")
        return

    if target.kind == KIND_SPACE:
        info(f"正在读取 UP 主 {target.mid} 的投稿列表…")
        items = list(api.space_videos(target.mid, opts.max_items))
        if not items:
            warn("没有取到投稿，接口可能触发风控，请稍后重试或配置 Cookie")
            return
        download_batch(items, api, dl, f"UP主_{target.mid}")
        return

    if target.kind == KIND_SEASON:
        info(f"正在读取合集 sid={target.season_id}…")
        items = list(api.season_archives(target.mid, target.season_id, opts.max_items))
        if not items:
            warn("合集为空或不可访问")
            return
        download_batch(items, api, dl, f"合集_{target.season_id}")
        return

    if target.kind == KIND_SEARCH:
        info(f"正在搜索「{target.keyword}」…")
        items = list(api.search(target.keyword, opts.max_items))
        if not items:
            warn("没有搜索到结果")
            return
        download_batch(items, api, dl, f"搜索_{target.keyword}")
        return

    raise ValueError(f"无法识别的目标：{target_text}")


def download_batch(items: List[Tuple[str, str]], api: BiliAPI, dl,
                   collection: str) -> None:
    """批量下载一组视频。"""
    total = len(items)
    info(f"共 {total} 个视频待处理")
    if not dl.opts.flat:
        dl.opts.collection_name = collection
    for i, (bvid, title) in enumerate(items, 1):
        info(f"[{i}/{total}] {bold(title or bvid)}  {dim(bvid)}")
        try:
            video = api.video_info(bvid=bvid)
            dl.download_video(video, dl.opts.pages)
        except BiliError as exc:
            dl.stats["fail"] += 1
            error(f"{bvid} 接口报错：{exc.message} (code={exc.code})")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            dl.stats["fail"] += 1
            error(f"{bvid} 失败：{exc}")
        if i < total:
            time.sleep(0.4)  # 轻微限速，避免触发风控
