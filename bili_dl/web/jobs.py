"""Web 界面的下载任务队列与状态管理。"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..api import BiliAPI, quality_label
from ..downloader import DownloadCancelled, Downloader, Options, find_ffmpeg
from ..runner import preview_target, run_target
from ..session import BiliError, HttpClient, RiskControlError
from ..utils import (debug, error, info, ok, sanitize_filename, set_log_sink,
                     warn)

MAX_LOGS = 400
HISTORY_LIMIT = 300

STATUS_TEXT = {
    "queued": "排队中",
    "running": "下载中",
    "done": "已完成",
    "error": "失败",
    "cancelled": "已取消",
}


@dataclass
class Job:
    """一个下载任务。"""

    id: str
    raw_input: str
    title: str = ""
    status: str = "queued"
    output_dir: str = ""
    quality: int = 127
    opts: Dict[str, Any] = field(default_factory=dict)

    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    progress_label: str = ""
    progress_done: int = 0
    progress_total: int = 0
    progress_speed: float = 0.0

    logs: List[Dict[str, Any]] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    error: str = ""
    stats: Dict[str, int] = field(default_factory=lambda: {"ok": 0, "skip": 0, "fail": 0})
    items_total: int = 0

    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _log_seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---------------------------------------------------------- 状态更新 ---
    def add_log(self, level: str, message: str) -> None:
        with self._lock:
            self._log_seq += 1
            self.logs.append({"seq": self._log_seq, "level": level,
                              "message": str(message)[:2000], "ts": time.time()})
            if len(self.logs) > MAX_LOGS:
                del self.logs[:-MAX_LOGS]

    def set_progress(self, label: str, done: int, total: int, speed: float) -> None:
        with self._lock:
            self.progress_label = label or self.progress_label
            self.progress_done = int(done)
            self.progress_total = int(total)
            self.progress_speed = float(speed or 0)

    def set_title(self, title: str) -> None:
        with self._lock:
            if title and not self.title:
                self.title = title

    # -------------------------------------------------------------- 序列化 ---
    def to_dict(self, since: int = 0, with_logs: bool = True) -> Dict[str, Any]:
        with self._lock:
            percent = 0.0
            if self.progress_total > 0:
                percent = min(100.0, self.progress_done * 100.0 / self.progress_total)
            data: Dict[str, Any] = {
                "id": self.id,
                "input": self.raw_input,
                "title": self.title or self.raw_input,
                "status": self.status,
                "status_text": STATUS_TEXT.get(self.status, self.status),
                "output_dir": self.output_dir,
                "quality": self.quality,
                "quality_label": quality_label(self.quality),
                "opts": self.opts,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed": (self.finished_at or time.time()) - (self.started_at or self.created_at),
                "progress": {
                    "label": self.progress_label,
                    "done": self.progress_done,
                    "total": self.progress_total,
                    "speed": self.progress_speed,
                    "percent": round(percent, 2),
                },
                "files": list(self.files),
                "error": self.error,
                "stats": dict(self.stats),
                "items_total": self.items_total,
                "log_seq": self._log_seq,
            }
            if with_logs:
                data["logs"] = [lg for lg in self.logs if lg["seq"] > since]
            return data

    def history_entry(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "input": self.raw_input,
            "title": self.title or self.raw_input,
            "status": self.status,
            "output_dir": self.output_dir,
            "quality_label": quality_label(self.quality),
            "files": list(self.files),
            "finished_at": self.finished_at or time.time(),
            "created_at": self.created_at,
        }


def build_options(opts: Dict[str, Any], output_dir: str, quality: int) -> Options:
    """把界面传来的选项字典变成 Downloader 的 Options。"""
    o = opts or {}
    return Options(
        output_dir=output_dir or "downloads",
        quality=int(quality or 127),
        codec=o.get("codec", "avc"),
        pages=o.get("pages") or None,
        workers=int(o.get("workers", 8) or 8),
        chunk_size=int(o.get("chunk_size", 8) or 8) * 1024 * 1024,
        audio_only=bool(o.get("audio_only")),
        video_only=bool(o.get("video_only")),
        format=o.get("format", "auto"),
        cover=bool(o.get("cover")),
        danmaku=bool(o.get("danmaku")),
        subtitle=bool(o.get("subtitle")),
        metadata=o.get("metadata", True) is not False,
        overwrite=bool(o.get("overwrite")),
        max_items=int(o.get("max_items", 0) or 0),
        flat=bool(o.get("flat")),
    )


class JobManager:
    """串起 HTTP 接口与 Downloader 的任务管理器。"""

    def __init__(self, client: HttpClient, api: BiliAPI, data_dir: str,
                 max_workers: int = 2):
        self.client = client
        self.api = api
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.history_path = os.path.join(data_dir, "history.json")
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max(1, max_workers),
                                        thread_name_prefix="bili-job")
        self._local = threading.local()
        self._history: List[Dict[str, Any]] = self._load_history()
        self.ffmpeg = find_ffmpeg()
        # 把 utils 里的日志/输出接进当前线程正在跑的任务
        set_log_sink(self._log_sink)

    # ------------------------------------------------------------ 日志汇 ---
    def _log_sink(self, level: str, message: str) -> None:
        job = getattr(self._local, "job", None)
        if job is not None:
            job.add_log(level, message)

    # ------------------------------------------------------------ 历史 ---
    def _load_history(self) -> List[Dict[str, Any]]:
        try:
            with open(self.history_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_history(self) -> None:
        try:
            tmp = self.history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._history[-HISTORY_LIMIT:], fh, ensure_ascii=False)
            os.replace(tmp, self.history_path)
        except Exception as exc:
            debug(f"写入历史记录失败：{exc}")

    def history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(reversed(self._history[-HISTORY_LIMIT:]))

    def clear_history(self) -> int:
        with self._lock:
            n = len(self._history)
            self._history = []
        self._save_history()
        return n

    # ------------------------------------------------------------ 任务 ---
    def create(self, raw_input: str, options: Dict[str, Any]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], raw_input=raw_input.strip())
        job.output_dir = options.get("output_dir", "")
        job.quality = int(options.get("quality", 127))
        # opts 里去掉不可序列化的东西
        job.opts = {k: v for k, v in options.items() if k != "output_dir"}
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        job.add_log("info", f"任务已创建：{job.raw_input}")
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self, with_logs: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = [self._jobs[j] for j in self._order if j in self._jobs]
        return [j.to_dict(with_logs=with_logs) for j in reversed(jobs)]

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status in ("done", "error", "cancelled"):
            return False
        job.cancel_event.set()
        job.add_log("warn", "已请求取消…")
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = time.time()
            self._record_history(job)
        return True

    def remove(self, job_id: str) -> bool:
        with self._lock:
            if job_id in self._jobs:
                if self._jobs[job_id].status == "running":
                    return False
                del self._jobs[job_id]
                if job_id in self._order:
                    self._order.remove(job_id)
                return True
        return False

    def _record_history(self, job: Job) -> None:
        with self._lock:
            self._history.append(job.history_entry())
            if len(self._history) > HISTORY_LIMIT:
                del self._history[:-HISTORY_LIMIT]
        self._save_history()

    # ------------------------------------------------------------ 执行 ---
    def _build_downloader(self, job: Job) -> Downloader:
        opts = build_options(job.opts, job.output_dir, job.quality)
        return Downloader(self.client, self.api, opts,
                          on_progress=job.set_progress,
                          cancel_event=job.cancel_event)

    def _run(self, job: Job) -> None:
        self._local.job = job
        job.status = "running"
        job.started_at = time.time()
        job.add_log("info", "开始处理…")
        dl: Optional[Downloader] = None
        try:
            dl = self._build_downloader(job)
            # 预览一次，拿到标题/条目数，进度界面更好看
            try:
                preview = preview_target(job.raw_input, self.api, self.client, limit=1)
                job.set_title(preview.get("title") or "")
                job.items_total = int(preview.get("total") or 1)
            except Exception as exc:
                debug(f"预解析失败（不影响下载）：{exc}")

            run_target(job.raw_input, self.api, dl, self.client)

            if dl:
                job.stats = dict(dl.stats)
            job.files = self._collect_files(job.output_dir, job.title)
            job.status = "done" if job.stats["fail"] == 0 else "error"
            if job.status == "error":
                job.error = f"{job.stats['fail']} 个条目下载失败"
        except DownloadCancelled:
            job.status = "cancelled"
            job.add_log("warn", "任务已取消（已下载的分片保留，可稍后续传）")
        except RiskControlError as exc:
            job.status = "error"
            job.error = str(exc)
            job.add_log("error", str(exc))
        except BiliError as exc:
            job.status = "error"
            job.error = f"{exc.message} (code={exc.code})"
            job.add_log("error", job.error)
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.add_log("error", job.error)
            if os.environ.get("BILI_DL_DEBUG"):
                import traceback
                traceback.print_exc()
        finally:
            job.finished_at = time.time()
            if job.status == "running":
                job.status = "done"
            job.add_log("info", f"任务结束：{STATUS_TEXT.get(job.status, job.status)}")
            self._record_history(job)
            self._local.job = None

    @staticmethod
    def _collect_files(output_dir: str, title: str, limit: int = 12) -> List[str]:
        """下载完成后，把本次产出的大致文件列表整理出来给界面用。"""
        out: List[str] = []
        if not output_dir or not os.path.isdir(output_dir):
            return out
        exts = (".mp4", ".m4a", ".m4s", ".flv")
        root = os.path.join(output_dir, sanitize_filename(title)) if title else output_dir
        roots = [root] if os.path.isdir(root) else [output_dir]
        for base in roots:
            for dirpath, _dirs, files in os.walk(base):
                for name in sorted(files):
                    if name.lower().endswith(exts):
                        out.append(os.path.join(dirpath, name))
                        if len(out) >= limit:
                            return out
                if out:
                    break
        return out
