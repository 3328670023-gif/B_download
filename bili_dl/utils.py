"""通用工具：日志、进度条、体积/时间格式化、文件名清洗。"""

from __future__ import annotations

import os
import re
import sys
import threading
import time

# ---------------------------------------------------------------- 输出着色 ---

_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()

# 日志汇：Web 界面会注册一个回调，把日志收进对应的下载任务里
_LOG_SINK = None


def set_log_sink(fn) -> None:
    """注册日志回调 fn(level, message)；传 None 恢复默认（只打印到终端）。"""
    global _LOG_SINK
    _LOG_SINK = fn


def _emit(level: str, message: str) -> None:
    if _LOG_SINK is None:
        return
    try:
        _LOG_SINK(level, message)
    except Exception:
        pass


def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(t):    return _c("2", t)
def bold(t):   return _c("1", t)
def red(t):    return _c("31", t)
def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def blue(t):   return _c("36", t)


def info(msg: str) -> None:
    _emit("info", msg)
    print(f"{blue('[信息]')} {msg}", flush=True)


def ok(msg: str) -> None:
    _emit("ok", msg)
    print(f"{green('[完成]')} {msg}", flush=True)


def warn(msg: str) -> None:
    _emit("warn", msg)
    print(f"{yellow('[警告]')} {msg}", flush=True, file=sys.stderr)


def error(msg: str) -> None:
    _emit("error", msg)
    print(f"{red('[错误]')} {msg}", flush=True, file=sys.stderr)


def debug(msg: str) -> None:
    if not os.environ.get("BILI_DL_DEBUG"):
        return
    _emit("debug", msg)
    print(dim(f"[调试] {msg}"), flush=True, file=sys.stderr)


# ------------------------------------------------------------ 格式化函数 ---

def human_size(n: float) -> str:
    """把字节数格式化成易读字符串。"""
    if n is None or n < 0:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    n = float(n)
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.0f}{units[i]}" if i == 0 else f"{n:.2f}{units[i]}"


def human_time(seconds: float) -> str:
    """把秒数格式化成 时:分:秒。"""
    if seconds is None or seconds < 0 or seconds != seconds:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def human_speed(bytes_per_sec: float) -> str:
    return f"{human_size(bytes_per_sec)}/s"


# -------------------------------------------------------------- 文件名 ---

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t\0]')
_MULTI_SPACE = re.compile(r"\s+")


def sanitize_filename(name: str, max_length: int = 120) -> str:
    """把任意标题清洗成安全的文件名（保留中文）。"""
    if not name:
        return "untitled"
    name = _ILLEGAL.sub("_", str(name))
    name = _MULTI_SPACE.sub(" ", name).strip().strip(".")
    # macOS/Linux 上以 . 开头会变隐藏文件；Windows 保留名也一并规避
    if name.startswith("-"):
        name = "_" + name[1:]
    if len(name.encode("utf-8")) > max_length:
        out, total = [], 0
        for ch in name:
            b = len(ch.encode("utf-8"))
            if total + b > max_length - 1:
                break
            out.append(ch)
            total += b
        name = "".join(out).rstrip() + "…"
    return name or "untitled"


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def short_path(path: str, limit: int = 70) -> str:
    """路径过长时省略中间部分，便于在终端里显示。"""
    if len(path) <= limit:
        return path
    keep = (limit - 3) // 2
    return f"{path[:keep]}...{path[-keep:]}"


# ---------------------------------------------------------------- 进度条 ---

class Progress:
    """线程安全的多任务进度条（渲染在终端同一行）。

    用法::

        p = Progress(total_bytes, "视频流")
        p.update(len(data))
        p.finish()
    """

    def __init__(self, total: int, label: str = "", stream=None, width: int = 28,
                 on_update=None):
        self.total = max(int(total or 0), 0)
        self.label = label
        self.done = 0
        self.width = width
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()
        self._start = time.time()
        self._last_render = 0.0
        self._finished = False
        # on_update(label, done, total, speed) —— Web 界面据此渲染进度条
        self._on_update = on_update

    # -- 内部 ---------------------------------------------------------
    def _render(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_render < 0.1:
            return
        self._last_render = now
        elapsed = max(now - self._start, 1e-6)
        speed = self.done / elapsed
        if self._on_update is not None:
            try:
                self._on_update(self.label, self.done, self.total, speed)
            except Exception:
                pass
        if self.total:
            ratio = min(self.done / self.total, 1.0)
            filled = int(self.width * ratio)
            bar = "█" * filled + "░" * (self.width - filled)
            pct = f"{ratio * 100:5.1f}%"
            eta = (self.total - self.done) / speed if speed > 0 else 0
            tail = f"{human_size(self.done)}/{human_size(self.total)}  {human_speed(speed)}  剩余 {human_time(eta)}"
        else:
            bar = "█" * (self.width // 4)
            pct = "  ?  "
            tail = f"{human_size(self.done)}  {human_speed(speed)}"
        label = f"{self.label} " if self.label else ""
        if _NO_COLOR or self._on_update is not None:
            return
        line = f"\r{label}[{bar}] {pct}  {tail}"
        # 用 \033[K 清掉本行残留
        self.stream.write(f"{line}\033[K")
        self.stream.flush()

    # -- 对外接口 -----------------------------------------------------
    def update(self, n: int) -> None:
        with self._lock:
            self.done += n
            self._render()

    def set_total(self, total: int) -> None:
        with self._lock:
            self.total = max(int(total or 0), 0)
            self._render(force=True)

    def finish(self, label: str | None = None) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self._render(force=True)
            if label is not None:
                self.label = label
            if not _NO_COLOR and self._on_update is None:
                self.stream.write("\n")
                self.stream.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.finish()
        return False


def retry(times: int = 3, delay: float = 1.5, backoff: float = 2.0,
          exceptions: tuple = (Exception,), on_error=None):
    """简单重试装饰器。"""

    def decorator(fn):
        def wrapper(*args, **kwargs):
            wait = delay
            last = None
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:  # noqa: PERF203
                    last = exc
                    if attempt == times:
                        break
                    if on_error:
                        on_error(attempt, exc)
                    debug(f"{fn.__name__} 第 {attempt} 次失败：{exc}，{wait:.1f}s 后重试")
                    time.sleep(wait)
                    wait *= backoff
            raise last  # type: ignore[misc]

        return wrapper

    return decorator
