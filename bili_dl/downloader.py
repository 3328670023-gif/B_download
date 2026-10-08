"""下载核心：码流选择、分片并发下载、断点续传、音视频合并。"""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import danmaku as danmaku_mod
from .api import (AUDIO_QUALITY_NAMES, BiliAPI, Page, Stream, Subtitle,
                  VideoInfo, quality_label)
from .session import BiliError, HttpClient
from .utils import (Progress, debug, ensure_dir, error, human_size, info, ok,
                    sanitize_filename, short_path, warn)

MEDIA_HEADERS = {"Referer": "https://www.bilibili.com/", "Accept": "*/*"}


class DownloadCancelled(Exception):
    """用户主动取消下载。"""

VIDEO_SUFFIX = ".mp4"
AUDIO_SUFFIX = ".m4a"

FFMPEG_CANDIDATES = [
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
    "/snap/bin/ffmpeg",
]


# ---------------------------------------------------------------- ffmpeg ---

def find_ffmpeg(explicit: str = "") -> str:
    """按顺序寻找可用的 ffmpeg。"""
    if explicit:
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return explicit
        found = shutil.which(explicit)
        if found:
            return found
        warn(f"指定的 ffmpeg 不可执行：{explicit}")
    env = os.environ.get("FFMPEG", "")
    if env and (os.path.isfile(env) or shutil.which(env)):
        return shutil.which(env) or env
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in FFMPEG_CANDIDATES:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    try:  # pip install imageio-ffmpeg 会附带一个静态 ffmpeg
        import imageio_ffmpeg  # type: ignore
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return ""


def _run_ffmpeg(ffmpeg: str, args: Sequence[str]) -> Tuple[bool, str]:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args]
    debug("执行：" + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, proc.stderr.decode("utf-8", "replace")[-800:]
    return True, ""


def merge_av(ffmpeg: str, video: str, audio: str, out: str) -> bool:
    """无损合并视频流与音频流。"""
    ok_, err = _run_ffmpeg(ffmpeg, [
        "-i", video, "-i", audio,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c", "copy", "-movflags", "+faststart",
        "-f", "mp4", out,
    ])
    if not ok_:
        error(f"ffmpeg 合并失败：{err.strip()[:400]}")
    return ok_


def remux_audio(ffmpeg: str, src: str, out: str) -> bool:
    ok_, err = _run_ffmpeg(ffmpeg, ["-i", src, "-vn", "-c:a", "copy", "-f", "mp4", out])
    if not ok_:
        error(f"ffmpeg 音频转封装失败：{err.strip()[:400]}")
    return ok_


def concat_files(ffmpeg: str, parts: Sequence[str], out: str, workdir: str) -> bool:
    """用 concat demuxer 拼接多个分片（多段 durl 时使用）。"""
    listfile = os.path.join(workdir, "concat.txt")
    with open(listfile, "w", encoding="utf-8") as fh:
        for p in parts:
            fh.write(f"file '{os.path.abspath(p)}'\n")
    ok_, err = _run_ffmpeg(ffmpeg, ["-f", "concat", "-safe", "0", "-i", listfile,
                                    "-c", "copy", "-movflags", "+faststart", out])
    if not ok_:
        error(f"ffmpeg 拼接失败：{err.strip()[:400]}")
    return ok_


# ------------------------------------------------------------ 资源探测 ---

def probe_resource(client: HttpClient, urls: Sequence[str],
                   fallback: int = 0) -> Tuple[int, bool, str]:
    """探测资源大小与是否支持 Range，返回 (size, ranges_ok, 可用URL)。"""
    last_err: Optional[Exception] = None
    for url in urls:
        if not url:
            continue
        try:
            with client.open(url, {**MEDIA_HEADERS, "Range": "bytes=0-0"}) as resp:
                cr = resp.headers.get("Content-Range") or ""
                resp.read(1)
            if cr and "/" in cr:
                total = cr.rsplit("/", 1)[-1].strip()
                if total.isdigit() and int(total) > 0:
                    return int(total), True, url
            # 服务器忽略 Range，返回 200
            return fallback, False, url
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                return 0, True, url
            last_err = exc
        except Exception as exc:
            last_err = exc
    if last_err:
        debug(f"探测资源失败：{last_err}")
    return fallback, True, urls[0] if urls else ""


# -------------------------------------------------------------- 分片下载 ---

@dataclass
class DownloadResult:
    path: str
    size: int
    ok: bool = True
    message: str = ""


class FileDownloader:
    """把一个 URL 下载到本地文件，支持多线程分片与断点续传。"""

    def __init__(self, client: HttpClient, workers: int = 8,
                 chunk_size: int = 8 * 1024 * 1024, retries: int = 4,
                 on_progress=None, cancel_event: Optional[threading.Event] = None):
        self.client = client
        self.workers = max(1, workers)
        self.chunk_size = max(512 * 1024, chunk_size)
        self.retries = retries
        self._lock = threading.Lock()
        # on_progress(label, done, total, speed) —— Web 界面用来画进度条
        self.on_progress = on_progress
        self.cancel_event = cancel_event

    def check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise DownloadCancelled("下载已取消")

    # -- 内部：带进度的区间下载 ---------------------------------------
    def _fetch_range(self, url: str, start: int, end: int,
                     progress: Progress) -> bytes:
        headers = {**MEDIA_HEADERS, "Range": f"bytes={start}-{end}"}
        expected = end - start + 1
        buf = bytearray()
        self.check_cancel()
        with self.client.open(url, headers, timeout=60) as resp:
            while True:
                block = resp.read(262144)
                if not block:
                    break
                buf.extend(block)
                progress.update(len(block))
                self.check_cancel()
        if len(buf) < expected:
            raise IOError(f"分片不完整 {len(buf)}/{expected}")
        return bytes(buf)

    def download(self, urls: Sequence[str], path: str, size_hint: int = 0,
                 label: str = "", refresh: Optional[Callable[[], List[str]]] = None
                 ) -> DownloadResult:
        ensure_dir(os.path.dirname(path) or ".")
        size, ranges_ok, url = probe_resource(self.client, urls, size_hint)
        if size <= 0 or not ranges_ok:
            return self._download_stream(url or urls[0], path, size, label)

        chunks = max(1, math.ceil(size / self.chunk_size))
        manifest_path = path + ".parts.json"
        done: set[int] = set()
        if os.path.exists(path) and os.path.getsize(path) == size and os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    done = {int(i) for i in json.load(fh).get("done", [])}
            except Exception:
                done = set()
        else:
            ensure_dir(os.path.dirname(path) or ".")
            with open(path, "wb") as fh:
                fh.truncate(size)
            if os.path.exists(manifest_path):
                os.remove(manifest_path)

        pending = [i for i in range(chunks) if i not in done]
        already = sum(min(self.chunk_size, size - i * self.chunk_size) for i in done)
        progress = Progress(size, label, on_update=self.on_progress)
        if already:
            progress.update(already)
            info(f"{label} 断点续传：已完成 {human_size(already)}/{human_size(size)}")
        else:
            info(f"{label} 共 {human_size(size)}，{chunks} 个分片，{self.workers} 线程")

        if not pending:
            progress.finish()
            self._cleanup_manifest(manifest_path)
            return DownloadResult(path, size)

        failures: List[int] = []
        current_url = [url]
        url_lock = threading.Lock()

        def worker(idx: int) -> int:
            start = idx * self.chunk_size
            end = min(size - 1, start + self.chunk_size - 1)
            last_exc: Optional[Exception] = None
            for attempt in range(1, self.retries + 1):
                try:
                    self.check_cancel()
                    with url_lock:
                        use = current_url[0]
                    data = self._fetch_range(use, start, end, progress)
                    with open(path, "r+b") as fh:
                        fh.seek(start)
                        fh.write(data)
                    return idx
                except DownloadCancelled:
                    raise
                except Exception as exc:
                    last_exc = exc
                    # 大概率是 URL 过期（403）或 CDN 抖动：换备用地址 / 刷新签名
                    if attempt == 2 and refresh is not None:
                        try:
                            fresh = refresh()
                            if fresh:
                                with url_lock:
                                    current_url[0] = fresh[0]
                                    alt = fresh
                                data = self._fetch_range(alt[attempt % len(alt)], start, end, progress)
                                with open(path, "r+b") as fh:
                                    fh.seek(start)
                                    fh.write(data)
                                return idx
                        except Exception as exc2:
                            last_exc = exc2
                    time.sleep(0.4 * attempt)
            debug(f"分片 {idx} 失败：{last_exc}")
            with self._lock:
                failures.append(idx)
            return -1

        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(worker, i) for i in pending]
                for fut in as_completed(futures):
                    idx = fut.result()
                    if idx >= 0:
                        done.add(idx)
                        self._save_manifest(manifest_path, done)
        finally:
            progress.finish()
        self.check_cancel()

        if failures:
            warn(f"{label} 有 {len(failures)} 个分片失败，已保留断点，重新运行可续传")
            return DownloadResult(path, size, ok=False,
                                  message=f"{len(failures)} 个分片失败")

        self._cleanup_manifest(manifest_path)
        return DownloadResult(path, size)

    # -- 单连接回退 -----------------------------------------------------
    def _download_stream(self, url: str, path: str, size: int, label: str) -> DownloadResult:
        start_at = os.path.getsize(path) if os.path.exists(path) else 0
        headers = dict(MEDIA_HEADERS)
        if start_at:
            headers["Range"] = f"bytes={start_at}-"
        progress = Progress(size, label, on_update=self.on_progress)
        if start_at:
            progress.update(start_at)
            info(f"{label} 断点续传：从 {human_size(start_at)} 继续")
        mode = "ab" if start_at else "wb"
        with self.client.open(url, headers, timeout=60) as resp, open(path, mode) as fh:
            if not size:
                cl = resp.headers.get("Content-Length")
                if cl and cl.isdigit():
                    size = int(cl) + start_at
                    progress.set_total(size)
            while True:
                block = resp.read(262144)
                if not block:
                    break
                fh.write(block)
                progress.update(len(block))
                self.check_cancel()
        progress.finish()
        return DownloadResult(path, os.path.getsize(path))

    @staticmethod
    def _save_manifest(path: str, done: set) -> None:
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"done": sorted(done)}, fh)
            os.replace(tmp, path)
        except Exception:
            pass

    @staticmethod
    def _cleanup_manifest(path: str) -> None:
        for p in (path, path + ".tmp"):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


# ------------------------------------------------------------ 码流选择 ---

def _codec_rank(codec: str, pref: str) -> int:
    """越小越优先。"""
    c = (codec or "").lower()
    table = {
        "avc": {"avc1": 0, "hev1": 1, "hvc1": 1, "av01": 2},
        "hevc": {"hev1": 0, "hvc1": 0, "avc1": 1, "av01": 2},
        "av1": {"av01": 0, "hev1": 1, "hvc1": 1, "avc1": 2},
    }
    order = table.get(pref, {"avc1": 0, "hev1": 1, "hvc1": 1, "av01": 2})
    for key, rank in order.items():
        if c.startswith(key):
            return rank
    return 9


def _to_stream(item: Dict[str, Any], kind: str, suffix: str = "m4s") -> Stream:
    urls = []
    base = item.get("baseUrl") or item.get("base_url") or ""
    if base:
        urls.append(base)
    for u in item.get("backupUrl") or item.get("backup_url") or []:
        if u and u not in urls:
            urls.append(u)
    return Stream(
        kind=kind,
        urls=urls,
        size=int(item.get("size") or 0),
        quality=int(item.get("id") or 0),
        codecs=str(item.get("codecs") or ""),
        bandwidth=int(item.get("bandwidth") or 0),
        width=int(item.get("width") or 0),
        height=int(item.get("height") or 0),
        suffix=suffix,
    )


def select_streams(data: Dict[str, Any], quality: int = 127, codec_pref: str = "avc",
                   need_video: bool = True, need_audio: bool = True
                   ) -> Tuple[Optional[Stream], Optional[Stream], List[str]]:
    """从 playurl 返回体里挑出视频流与音频流。

    返回 (video_stream, audio_stream, 可用清晰度描述列表)。
    """
    notes: List[str] = []
    video: Optional[Stream] = None
    audio: Optional[Stream] = None

    dash = data.get("dash") or {}
    if dash and (need_video or need_audio):
        vlist = list(dash.get("video") or [])
        if need_video and vlist:
            available = sorted({int(v.get("id") or 0) for v in vlist}, reverse=True)
            notes = [quality_label(q) for q in available]
            target = quality
            if target >= 127:
                chosen_q = available[0]
            else:
                candidates = [q for q in available if q <= target]
                chosen_q = candidates[0] if candidates else available[-1]
                if chosen_q != target:
                    notes.append(f"（请求 {quality_label(target)}，实际最高可用 {quality_label(chosen_q)}）")
            same_q = [v for v in vlist if int(v.get("id") or 0) == chosen_q]
            if codec_pref == "best":
                best = max(same_q, key=lambda v: int(v.get("bandwidth") or 0))
            else:
                best = min(same_q, key=lambda v: (_codec_rank(v.get("codecs", ""), codec_pref),
                                                  -int(v.get("bandwidth") or 0)))
            video = _to_stream(best, "video")

        if need_audio:
            alist = list(dash.get("audio") or [])
            for extra_key in ("flac", "dolby"):
                extra = (dash.get(extra_key) or {}).get("audio")
                if isinstance(extra, dict) and extra:
                    alist.append(extra)
                elif isinstance(extra, list):
                    alist.extend(extra)
            if alist:
                best_a = max(alist, key=lambda a: int(a.get("bandwidth") or 0))
                audio = _to_stream(best_a, "audio")
    return video, audio, notes


def progressive_stream(data: Dict[str, Any]) -> Optional[Stream]:
    """fnval=1 时返回的是音视频合一的 MP4 分段列表。"""
    durl = data.get("durl") or []
    if not durl:
        return None
    urls: List[str] = []
    total = 0
    for seg in durl:
        u = seg.get("url")
        if u:
            urls.append(u)
        total += int(seg.get("size") or 0)
        for bu in seg.get("backup_url") or []:
            if bu:
                urls.append(bu)
    if not urls:
        return None
    return Stream(kind="progressive", urls=urls, size=total,
                  quality=int(data.get("quality") or 0),
                  codecs=data.get("format", "mp4"), suffix="mp4")


# ---------------------------------------------------------------- 选项 ---

@dataclass
class Options:
    output_dir: str = "downloads"
    quality: int = 127
    codec: str = "avc"
    pages: Optional[List[int]] = None   # None = 全部
    workers: int = 8
    chunk_size: int = 8 * 1024 * 1024
    audio_only: bool = False
    video_only: bool = False
    format: str = "auto"                # auto | dash | mp4
    cover: bool = False
    danmaku: bool = False
    subtitle: bool = False
    metadata: bool = True
    overwrite: bool = False
    dry_run: bool = False
    keep_temp: bool = False
    ffmpeg: str = ""
    max_items: int = 0
    collection_name: str = ""           # 批量下载时的子目录名
    flat: bool = False                  # 批量下载时不建子目录


# ------------------------------------------------------------ 下载器 ---

class Downloader:
    def __init__(self, client: HttpClient, api: BiliAPI, opts: Options,
                 on_progress=None, cancel_event: Optional[threading.Event] = None):
        self.client = client
        self.api = api
        self.opts = opts
        self.cancel_event = cancel_event
        self.fetcher = FileDownloader(client, opts.workers, opts.chunk_size,
                                      on_progress=on_progress,
                                      cancel_event=cancel_event)
        self.ffmpeg = find_ffmpeg(opts.ffmpeg)
        self._ffmpeg_warned = False
        self.stats = {"ok": 0, "skip": 0, "fail": 0}

    # ------------------------------------------------------------ 工具 ---
    def _note_ffmpeg_missing(self) -> None:
        if self._ffmpeg_warned or self.ffmpeg:
            return
        self._ffmpeg_warned = True
        warn("未检测到 ffmpeg，DASH 音视频无法自动合并。\n"
             "       安装方法：brew install ffmpeg\n"
             "       或：python3 -m pip install imageio-ffmpeg\n"
             "       也可用 --ffmpeg /path/to/ffmpeg 指定路径")

    def _out_root(self) -> str:
        root = self.opts.output_dir
        if self.opts.collection_name and not self.opts.flat:
            root = os.path.join(root, sanitize_filename(self.opts.collection_name))
        return ensure_dir(root)

    def _plan_paths(self, video: VideoInfo, page: Page) -> Tuple[str, str]:
        """返回 (目录, 文件名主干)。"""
        root = self._out_root()
        title = sanitize_filename(video.title)
        if video.is_pgc and not title:
            title = sanitize_filename(f"番剧_{video.season_id or video.ep_id}")
        if len(video.pages) > 1:
            folder = ensure_dir(os.path.join(root, title))
            part = sanitize_filename(page.title) if page.title else ""
            stem = f"P{page.index:02d}"
            if part and part.lower() not in ("", title.lower()):
                stem = f"{stem} {part}"
            return folder, sanitize_filename(stem)
        return root, title

    @staticmethod
    def _already_done(folder: str, stem: str, exts: Sequence[str]) -> Optional[str]:
        for ext in exts:
            p = os.path.join(folder, stem + ext)
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return p
        return None

    # ------------------------------------------------------ 单个视频 ---
    def download_video(self, video: VideoInfo, pages: Optional[List[int]] = None) -> None:
        pages = self._select_pages(video, pages)
        for i, page in enumerate(pages, 1):
            if len(pages) > 1:
                info(f"[{i}/{len(pages)}] {video.title} —— P{page.index} {page.title}")
            try:
                self.download_page(video, page)
            except DownloadCancelled:
                raise
            except BiliError as exc:
                self.stats["fail"] += 1
                error(f"P{page.index} 接口报错：{exc.message} (code={exc.code})")
                if exc.code in (-404, 62002):
                    warn("该视频可能已失效、被删除或需要大会员权限")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self.stats["fail"] += 1
                error(f"P{page.index} 下载失败：{exc}")

    @staticmethod
    def _select_pages(video: VideoInfo, pages: Optional[List[int]]) -> List[Page]:
        if not pages:
            return list(video.pages)
        out = []
        for idx in pages:
            p = video.page_by_index(idx)
            if p:
                out.append(p)
            else:
                warn(f"视频没有第 {idx} P，已忽略")
        return out or list(video.pages)

    # ------------------------------------------------------ 单个分P ---
    def download_page(self, video: VideoInfo, page: Page) -> None:
        folder, stem = self._plan_paths(video, page)
        opts = self.opts

        want_exts = [AUDIO_SUFFIX] if opts.audio_only else (
            [".m4s"] if (opts.video_only and not self.ffmpeg) else [VIDEO_SUFFIX, ".m4s"])
        existing = None if opts.overwrite else self._already_done(folder, stem, want_exts)
        if existing and not opts.overwrite:
            info(f"已存在，跳过：{short_path(existing)}")
            self.stats["skip"] += 1
            return

        # ---- 选择下载通道（DASH 分离流 / MP4 合流）----------------------
        mode, data = self._choose_format(video, page)

        if opts.dry_run:
            self._print_dry_run(video, page, data, mode)
            return

        if mode == "progressive":
            stream = progressive_stream(data)
            if stream is not None:
                self._download_progressive(stream, data, folder, stem, video, page)
                return
            warn("该视频没有可用的 MP4 合流，改用 DASH 分离流")
            mode, data = "dash", self._get_playurl(video, page, 4048)

        if not self.ffmpeg:
            self._note_ffmpeg_missing()

        video_stream, audio_stream, notes = select_streams(
            data, opts.quality, opts.codec,
            need_video=not opts.audio_only, need_audio=not opts.video_only)
        if notes:
            debug("可用清晰度：" + "、".join(notes))
        if video_stream is None and audio_stream is None:
            raise RuntimeError("未能从接口返回中解析出可下载的媒体流")

        if video_stream:
            info(f"画质：{video_stream.label}")

        temp = ensure_dir(os.path.join(folder, ".bili_tmp"))
        cid_tag = f"{video.bvid or 'av' + str(video.aid)}_{page.cid}"

        def refresh(stream: Stream) -> Callable[[], List[str]]:
            def _refresh() -> List[str]:
                debug("刷新播放地址（URL 可能已过期）")
                try:
                    new_data = self._get_playurl(video, page, 4048)
                    v2, a2, _ = select_streams(new_data, opts.quality, opts.codec,
                                               need_video=stream.kind == "video",
                                               need_audio=stream.kind == "audio")
                    pick = v2 if stream.kind == "video" else a2
                    if pick and pick.urls:
                        stream.urls = pick.urls
                        return pick.urls
                except Exception as exc:
                    debug(f"刷新失败：{exc}")
            return _refresh

        # ---- 仅音频 -----------------------------------------------------
        if opts.audio_only:
            if not audio_stream:
                raise RuntimeError("没有可用的音频流（可能需要大会员权限）")
            info(f"音频：{audio_stream.label}")
            raw = os.path.join(temp, f"{cid_tag}.audio.m4s")
            res = self.fetcher.download(audio_stream.urls, raw,
                                        size_hint=audio_stream.size,
                                        label="音频流", refresh=refresh(audio_stream))
            if not res.ok:
                self.stats["fail"] += 1
                return
            out = os.path.join(folder, stem + AUDIO_SUFFIX)
            if self.ffmpeg and remux_audio(self.ffmpeg, raw, out):
                ok(f"已保存：{short_path(out)}")
            else:
                out = os.path.join(folder, stem + ".m4s")
                shutil.move(raw, out)
                ok(f"已保存（原始音频流）：{short_path(out)}")
            self._sidecars(folder, stem, video, page, temp)
            self.stats["ok"] += 1
            return

        # ---- 仅视频 -----------------------------------------------------
        if opts.video_only:
            if not video_stream:
                raise RuntimeError("没有可用的视频流")
            raw = os.path.join(temp, f"{cid_tag}.video.m4s")
            res = self.fetcher.download(video_stream.urls, raw,
                                        size_hint=video_stream.size,
                                        label="视频流", refresh=refresh(video_stream))
            if not res.ok:
                self.stats["fail"] += 1
                return
            if self.ffmpeg:
                out = os.path.join(folder, stem + "_video.mp4")
                success, err = _run_ffmpeg(self.ffmpeg, ["-i", raw, "-c", "copy",
                                                         "-movflags", "+faststart", out])
                if not success:
                    out = os.path.join(folder, stem + ".video.m4s")
                    shutil.move(raw, out)
            else:
                out = os.path.join(folder, stem + ".video.m4s")
                shutil.move(raw, out)
            ok(f"已保存：{short_path(out)}")
            self.stats["ok"] += 1
            return

        # ---- 完整视频（DASH 分离流 + 合并） -----------------------------
        if not video_stream:
            raise RuntimeError("没有可用的视频流")
        vraw = os.path.join(temp, f"{cid_tag}.video.m4s")
        res = self.fetcher.download(video_stream.urls, vraw,
                                    size_hint=video_stream.size,
                                    label="视频流", refresh=refresh(video_stream))
        if not res.ok:
            self.stats["fail"] += 1
            return

        araw = ""
        if audio_stream:
            info(f"音频：{audio_stream.label}")
            araw = os.path.join(temp, f"{cid_tag}.audio.m4s")
            ares = self.fetcher.download(audio_stream.urls, araw,
                                         size_hint=audio_stream.size,
                                         label="音频流", refresh=refresh(audio_stream))
            if not ares.ok:
                self.stats["fail"] += 1
                return

        if self.ffmpeg and araw:
            out = os.path.join(folder, stem + VIDEO_SUFFIX)
            info("正在合并音视频…")
            if merge_av(self.ffmpeg, vraw, araw, out) and os.path.getsize(out) > 0:
                ok(f"已保存：{short_path(out)}")
                self.stats["ok"] += 1
                self._sidecars(folder, stem, video, page, temp)
                self._cleanup_temp(temp, keep=opts.keep_temp)
                return
            warn("合并失败，保留原始流文件")
        if self.ffmpeg and not araw:
            out = os.path.join(folder, stem + VIDEO_SUFFIX)
            success, _ = _run_ffmpeg(self.ffmpeg, ["-i", vraw, "-c", "copy",
                                                   "-movflags", "+faststart", out])
            if success:
                ok(f"已保存：{short_path(out)}")
                self.stats["ok"] += 1
                self._sidecars(folder, stem, video, page, temp)
                self._cleanup_temp(temp, keep=opts.keep_temp)
                return

        # 无 ffmpeg：保留原始流并给出合并命令
        vout = os.path.join(folder, stem + ".video.m4s")
        shutil.move(vraw, vout)
        aout = ""
        if araw:
            aout = os.path.join(folder, stem + ".audio.m4s")
            shutil.move(araw, aout)
        self.stats["ok"] += 1
        warn(f"已保存原始流（未合并）：{short_path(vout)}" + (f" + {short_path(aout)}" if aout else ""))
        if aout:
            print(f"       合并命令：ffmpeg -i {shlex_quote(vout)} -i {shlex_quote(aout)} "
                  f"-c copy {shlex_quote(os.path.join(folder, stem + VIDEO_SUFFIX))}")
        self._sidecars(folder, stem, video, page, temp)
        self._cleanup_temp(temp, keep=opts.keep_temp)

    # --------------------------------------------------- MP4 合流下载 ---
    def _download_progressive(self, stream: Stream, data: Dict[str, Any], folder: str,
                              stem: str, video: VideoInfo, page: Page) -> None:
        info(f"画质：{quality_label(stream.quality)}（MP4 合流，无需 ffmpeg）")
        temp = ensure_dir(os.path.join(folder, ".bili_tmp"))
        parts: List[str] = []
        durl = data.get("durl") or []
        for i, seg in enumerate(durl, 1):
            urls = [seg.get("url")] + list(seg.get("backup_url") or [])
            urls = [u for u in urls if u]
            if not urls:
                continue
            part_path = os.path.join(temp, f"{stem}.part{i:02d}.mp4")
            res = self.fetcher.download(urls, part_path, size_hint=int(seg.get("size") or 0),
                                        label=f"视频（第{i}段）" if len(durl) > 1 else "视频")
            if not res.ok:
                self.stats["fail"] += 1
                return
            parts.append(part_path)
        if not parts:
            raise RuntimeError("播放地址为空")
        out = os.path.join(folder, stem + VIDEO_SUFFIX)
        if len(parts) == 1:
            shutil.move(parts[0], out)
        else:
            if self.ffmpeg:
                if not concat_files(self.ffmpeg, parts, out, temp):
                    out = os.path.join(folder, stem + ".part1.mp4")
                    shutil.move(parts[0], out)
            else:
                warn("多段 MP4 且没有 ffmpeg，只保留第一段")
                out = os.path.join(folder, stem + ".part1.mp4")
                shutil.move(parts[0], out)
        ok(f"已保存：{short_path(out)}")
        self.stats["ok"] += 1
        self._sidecars(folder, stem, video, page, temp)
        self._cleanup_temp(temp, keep=self.opts.keep_temp)

    # ------------------------------------------------------ 播放地址 ---
    def _get_playurl(self, video: VideoInfo, page: Page,
                     fnval: int = 0) -> Dict[str, Any]:
        """fnval=4048 取 DASH 分离流；fnval=1 取音视频合一的 MP4。"""
        opts = self.opts
        if not fnval:
            fnval = 4048 if (self.ffmpeg or opts.audio_only or opts.video_only) else 1
        # MP4 合流最高只到 1080P，qn 超过 80 没有意义
        qn = min(opts.quality, 80) if fnval == 1 else opts.quality
        if video.is_pgc:
            ep_id = self._ep_id_for(video, page)
            return self.api.pgc_playurl(ep_id=ep_id, cid=page.cid, qn=qn, fnval=fnval)
        return self.api.playurl(cid=page.cid, qn=qn, fnval=fnval,
                                bvid=video.bvid, avid=video.aid)

    def _choose_format(self, video: VideoInfo, page: Page) -> Tuple[str, Dict[str, Any]]:
        """在 DASH 与 MP4 合流之间挑出实际画质更高的那一条通道。

        B 站对匿名用户并不总是「DASH 更清晰」：同一个视频可能 MP4 合流有 720P、
        而 DASH 只给到 480P。所以 auto 模式下两种都问一次再比较。
        """
        opts = self.opts
        if opts.audio_only or opts.video_only:
            return "dash", self._get_playurl(video, page, 4048)
        if opts.format == "mp4":
            return "progressive", self._get_playurl(video, page, 1)
        if opts.format == "dash":
            return "dash", self._get_playurl(video, page, 4048)

        dash_data = self._get_playurl(video, page, 4048)
        dash_v, _, _ = select_streams(dash_data, opts.quality, opts.codec,
                                      need_video=True, need_audio=False)
        dash_q = dash_v.quality if dash_v else 0
        dash_label = quality_label(dash_q) if dash_q else "无"

        # 已登录且装了 ffmpeg 时 DASH 一定不差，省掉一次请求
        if self.ffmpeg and self.api.is_login():
            return "dash", dash_data

        prog_data = self._get_playurl(video, page, 1)
        stream = progressive_stream(prog_data)
        prog_q = stream.quality if stream else 0

        if self.ffmpeg:
            if stream is not None and prog_q > dash_q:
                debug(f"MP4 合流画质更高（{quality_label(prog_q)} > {dash_label}），改用合流")
                return "progressive", prog_data
            return "dash", dash_data

        # 没有 ffmpeg：只能靠 MP4 合流（除非合流不可用）
        if stream is not None:
            if prog_q < dash_q:
                warn(f"DASH 还能提供 {dash_label}，但需要 ffmpeg 合并音视频；"
                     f"本次按 MP4 合流下载 {quality_label(prog_q)}")
            return "progressive", prog_data
        return "dash", dash_data

    def inspect(self, video: VideoInfo, page: Page) -> Dict[str, Any]:
        """给界面用：列出两条通道的可用画质，以及 auto 模式最终会怎么选。"""
        opts = self.opts
        out: Dict[str, Any] = {"dash": [], "progressive": None, "auto": None,
                               "ffmpeg": bool(self.ffmpeg)}
        dash_data: Dict[str, Any] = {}
        prog_data: Dict[str, Any] = {}
        errors: List[str] = []
        try:
            dash_data = self._get_playurl(video, page, 4048)
        except Exception as exc:
            errors.append(f"DASH：{exc}")
        try:
            prog_data = self._get_playurl(video, page, 1)
        except Exception as exc:
            errors.append(f"合流：{exc}")
        if not dash_data and not prog_data:
            raise RuntimeError("；".join(errors) or "无法获取播放地址")

        dash = dash_data.get("dash") or {}
        seen: Dict[int, Dict[str, Any]] = {}
        for v in dash.get("video") or []:
            qn = int(v.get("id") or 0)
            item = seen.setdefault(qn, {"qn": qn, "label": quality_label(qn),
                                        "height": int(v.get("height") or 0),
                                        "width": int(v.get("width") or 0),
                                        "codecs": [], "bandwidth": 0})
            codec = str(v.get("codecs") or "")
            if codec and codec not in item["codecs"]:
                item["codecs"].append(codec)
            item["bandwidth"] = max(item["bandwidth"], int(v.get("bandwidth") or 0))
        out["dash"] = sorted(seen.values(), key=lambda x: -x["qn"])

        pstream = progressive_stream(prog_data)
        if pstream is not None:
            out["progressive"] = {"qn": pstream.quality,
                                  "label": quality_label(pstream.quality),
                                  "size": pstream.size}

        mode, data = self._choose_format(video, page)
        if mode == "progressive":
            st = progressive_stream(data)
            out["auto"] = {"mode": "progressive",
                           "qn": st.quality if st else 0,
                           "label": quality_label(st.quality) if st else "未知"}
        else:
            v2, _a2, _notes = select_streams(
                data, opts.quality, opts.codec,
                need_video=not opts.audio_only, need_audio=not opts.video_only)
            out["auto"] = {"mode": "dash", "qn": v2.quality if v2 else 0,
                           "label": v2.label if v2 else "未知"}
        return out

    def _ep_id_for(self, video: VideoInfo, page: Page) -> int:
        # ss 号下载时每个分P自带 ep_id；单集链接则用 video.ep_id
        return int(page.ep_id or video.ep_id or 0)

    # ---------------------------------------------------------- 附加 ---
    def _sidecars(self, folder: str, stem: str, video: VideoInfo, page: Page,
                  temp: str) -> None:
        opts = self.opts
        if opts.metadata:
            self._write_metadata(folder, stem, video, page)
        if opts.cover:
            self._write_cover(folder, stem, video)
        if opts.subtitle:
            self._write_subtitles(folder, stem, video, page)
        if opts.danmaku:
            self._write_danmaku(folder, stem, page)

    def _write_metadata(self, folder: str, stem: str, video: VideoInfo, page: Page) -> None:
        meta = {
            "bvid": video.bvid,
            "aid": video.aid,
            "cid": page.cid,
            "page": page.index,
            "page_title": page.title,
            "title": video.title,
            "owner": video.owner,
            "owner_mid": video.owner_mid,
            "duration": page.duration or video.duration,
            "pubdate": video.pubdate,
            "cover": video.cover,
            "desc": video.desc,
            "url": (f"https://www.bilibili.com/video/{video.bvid}"
                    + (f"?p={page.index}" if len(video.pages) > 1 else "")) if video.bvid
                   else f"https://www.bilibili.com/bangumi/play/ep{video.ep_id}",
            "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "downloader": "bili-dl",
        }
        path = os.path.join(folder, stem + ".info.json")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            debug(f"写入元数据失败：{exc}")

    def _write_cover(self, folder: str, stem: str, video: VideoInfo) -> None:
        if not video.cover:
            return
        ext = ".jpg"
        path = os.path.join(folder, stem + ext)
        if os.path.exists(path) and not self.opts.overwrite:
            return
        try:
            data = self.client.get_bytes(video.cover, retries=2)
            with open(path, "wb") as fh:
                fh.write(data)
            debug(f"封面已保存：{path}")
        except Exception as exc:
            debug(f"封面下载失败：{exc}")

    def _write_subtitles(self, folder: str, stem: str, video: VideoInfo, page: Page) -> None:
        subs = self.api.subtitle_list(video.bvid, page.cid, video.aid)
        if not subs:
            debug("该视频没有可用 CC 字幕（未登录时通常为空）")
            return
        for sub in subs:
            try:
                payload = self.client.get_json(sub.url, retries=2)
            except Exception as exc:
                debug(f"字幕下载失败 {sub.lan}: {exc}")
                continue
            body = payload.get("body") or []
            if not body:
                continue
            path = os.path.join(folder, f"{stem}.{sanitize_filename(sub.lan_doc or sub.lan)}.srt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(srt_from_bili(body))
            debug(f"字幕已保存：{path}")

    def _write_danmaku(self, folder: str, stem: str, page: Page) -> None:
        items = danmaku_mod.fetch_danmaku(self.client, page.cid)
        if not items:
            debug("未获取到弹幕")
            return
        path = os.path.join(folder, stem + ".xml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(danmaku_mod.to_xml(items, cid=page.cid))
        info(f"弹幕已保存（{len(items)} 条）：{short_path(path)}")

    def _cleanup_temp(self, temp: str, keep: bool = False) -> None:
        if keep:
            info(f"临时文件保留在：{short_path(temp)}")
            return
        shutil.rmtree(temp, ignore_errors=True)

    # -------------------------------------------------------- 预演 ---
    def _print_dry_run(self, video: VideoInfo, page: Page,
                       data: Dict[str, Any], mode: str = "dash") -> None:
        folder, stem = self._plan_paths(video, page)
        print(f"\n  \033[1m{video.title}\033[0m"
              + (f"  P{page.index} {page.title}" if len(video.pages) > 1 else ""))
        if video.owner:
            print(f"  UP主：{video.owner}    时长：{page.duration or video.duration}s")
        print(f"    下载通道：{'MP4 合流（无需 ffmpeg）' if mode == 'progressive' else 'DASH 分离流'}")
        dash = data.get("dash") or {}
        if mode == "progressive":
            stream = progressive_stream(data)
            print(f"    · 可选画质：{quality_label(stream.quality) if stream else '无'}")
        elif dash:
            seen: Dict[int, List[str]] = {}
            for v in dash.get("video") or []:
                seen.setdefault(int(v.get("id") or 0), []).append(
                    f"{v.get('codecs')} {v.get('width')}x{v.get('height')} "
                    f"{int(v.get('bandwidth') or 0) // 1000}kbps")
            for q in sorted(seen, reverse=True):
                print(f"    · {quality_label(q)}: " + " | ".join(seen[q]))
            audios = dash.get("audio") or []
            if audios:
                best = max(audios, key=lambda a: int(a.get("bandwidth") or 0))
                print(f"    · 音频：{AUDIO_QUALITY_NAMES.get(int(best.get('id') or 0), best.get('id'))} "
                      f"{best.get('codecs')} {int(best.get('bandwidth') or 0) // 1000}kbps")
        else:
            print(f"    · 仅 MP4 合流：{quality_label(int(data.get('quality') or 0))}")
        print(f"    输出：{os.path.join(folder, stem)}.mp4")


# ---------------------------------------------------------------- 辅助 ---

def shlex_quote(text: str) -> str:
    return shlex.quote(text)


def _srt_time(seconds: float) -> str:
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_from_bili(body: Sequence[Dict[str, Any]]) -> str:
    """把 B 站字幕 JSON 转成 SRT。"""
    lines: List[str] = []
    for i, item in enumerate(body, 1):
        try:
            start = float(item.get("from", 0))
            end = float(item.get("to", 0))
        except (TypeError, ValueError):
            continue
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        lines.append(str(i))
        lines.append(f"{_srt_time(start)} --> {_srt_time(end)}")
        lines.append(content)
        lines.append("")
    return "\n".join(lines)
