"""本地 Web 服务：把 bili-dl 包装成一个可以在浏览器里用的下载应用。

安全性：只监听 127.0.0.1，所有 /api/* 请求都要带启动时随机生成的 token，
并校验 Host 头（防止 DNS rebinding），确保只有本机能访问。
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from .. import __version__
from ..api import BiliAPI, quality_label
from ..downloader import Downloader, find_ffmpeg
from ..session import BiliError, HttpClient, RiskControlError
from ..utils import debug, error, info, ok, sanitize_filename, warn
from ..runner import preview_target
from .jobs import JobManager, build_options

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

QUALITY_CHOICES: List[Tuple[int, str]] = [
    (127, "8K 超高清"), (126, "杜比视界"), (125, "HDR 真彩"), (120, "4K 超清"),
    (116, "1080P60 高帧率"), (112, "1080P+ 高码率"), (80, "1080P 高清"),
    (74, "720P60 高帧率"), (64, "720P 准高清"), (32, "480P 标清"),
    (16, "360P 流畅"), (6, "240P 极速"),
]


def default_output_dir() -> str:
    """默认下载目录：优先用外置硬盘上的专用目录。"""
    candidates = [
        "/Volumes/1B的硬盘1/bilibili下载",
        os.path.join(os.path.expanduser("~"), "Movies", "bilibili"),
    ]
    for path in candidates:
        parent = os.path.dirname(path)
        if os.path.isdir(parent) and os.access(parent, os.W_OK):
            return path
    return os.path.join(os.path.dirname(STATIC_DIR), "..", "..", "downloads")


class App:
    """应用状态：设置项 + 任务队列。"""

    def __init__(self, client: HttpClient, api: BiliAPI, data_dir: str,
                 token: str = "", max_workers: int = 2):
        self.client = client
        self.api = api
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.settings_path = os.path.join(data_dir, "settings.json")
        self.token = (token or os.environ.get("BILI_DL_WEB_TOKEN")
                      or secrets.token_urlsafe(24))
        self.settings = self._load_settings()
        self.jobs = JobManager(client, api, data_dir, max_workers=max_workers)
        self.ffmpeg = find_ffmpeg()

    # ------------------------------------------------------------ 设置 ---
    def _load_settings(self) -> Dict[str, Any]:
        base = {
            "output_dir": default_output_dir(),
            "quality": 127,
            "codec": "avc",
            "workers": 8,
            "chunk_size": 8,
            "max_items": 0,
            "cover": True,
            "danmaku": False,
            "subtitle": False,
            "audio_only": False,
            "video_only": False,
            "flat": False,
            "overwrite": False,
            "format": "auto",
        }
        try:
            with open(self.settings_path, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                base.update({k: v for k, v in saved.items() if k in base})
        except Exception:
            pass
        if not base.get("output_dir"):
            base["output_dir"] = default_output_dir()
        return base

    def save_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        allowed = set(self.settings.keys())
        for key, value in (patch or {}).items():
            if key in allowed:
                self.settings[key] = value
            elif key == "output_dir":
                self.settings[key] = value
        self.settings["output_dir"] = os.path.expanduser(
            str(self.settings.get("output_dir") or default_output_dir()))
        try:
            tmp = self.settings_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.settings, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.settings_path)
        except Exception as exc:
            warn(f"保存设置失败：{exc}")
        return self.settings

    # ------------------------------------------------------------ 状态 ---
    def state(self) -> Dict[str, Any]:
        me = self.api.self_info()
        return {
            "version": __version__,
            "ffmpeg": self.ffmpeg or "",
            "ffmpeg_ok": bool(self.ffmpeg),
            "logged_in": bool(me.get("isLogin")),
            "uname": me.get("uname", ""),
            "vip": bool((me.get("vipStatus") or 0) == 1) if isinstance(me.get("vipStatus"), int)
            else bool(me.get("vipStatus")),
            "settings": dict(self.settings),
            "qualities": [{"value": q, "label": f"{name}（qn{q}）"}
                          for q, name in QUALITY_CHOICES],
            "hostname": socket.gethostname(),
        }

    # ------------------------------------------------------------ 解析 ---
    def parse(self, text: str) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ValueError("请输入视频链接、BV号或搜索关键字")
        limit = int(self.settings.get("max_items") or 0) or 50
        data, ctx = preview_target(text, self.api, self.client, limit=limit,
                                   with_context=True)

        # 单视频再补一份「各通道可用画质」，界面上就能直接挑
        if data.get("kind") == "video" and ctx.get("video") and ctx.get("page"):
            try:
                opts = build_options(self.settings, self.settings["output_dir"],
                                     int(self.settings.get("quality", 127)))
                dl = Downloader(self.client, self.api, opts)
                data["qualities"] = dl.inspect(ctx["video"], ctx["page"])
            except RiskControlError as exc:
                data["qualities"] = None
                data["qualities_error"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                debug(f"探测可用画质失败：{exc}")
                data["qualities"] = None
                data["qualities_error"] = f"{type(exc).__name__}: {exc}"
        return data

    # ------------------------------------------------------------ 任务 ---
    def create_job(self, text: str, options: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(self.settings)
        merged.update({k: v for k, v in (options or {}).items() if v is not None})
        job = self.jobs.create(text, merged)
        return job.to_dict()

    def reveal(self, path: str) -> None:
        """在访达里显示文件/目录（只允许打开下载目录内的路径）。"""
        import subprocess
        allowed = os.path.realpath(self.settings["output_dir"])
        target = os.path.realpath(os.path.expanduser(path or allowed))
        # 允许打开输出目录本身、其父目录，以及目录内的任意路径
        if not (target == allowed or target.startswith(allowed + os.sep)
                or allowed.startswith(target + os.sep) or target == os.path.dirname(allowed)):
            raise PermissionError("只允许打开下载目录内的文件")
        if not os.path.exists(target):
            if os.path.isdir(os.path.dirname(target)):
                target = os.path.dirname(target)
            else:
                raise FileNotFoundError(f"路径不存在：{target}")
        if os.name == "posix" and os.uname().sysname == "Darwin":
            subprocess.Popen(["open", "-R" if os.path.isfile(target) else "", target]
                             if os.path.isfile(target) else ["open", target])
        else:
            webbrowser.open(f"file://{target}")


class Handler(BaseHTTPRequestHandler):
    server_version = "bili-dl-web"
    protocol_version = "HTTP/1.1"
    app: App = None  # type: ignore[assignment]

    # ------------------------------------------------------------ 基础 ---
    def log_message(self, fmt: str, *args) -> None:
        debug("HTTP " + (fmt % args))

    def _send(self, code: int, body: bytes, ctype: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: Any, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _ok(self, data: Any = None) -> None:
        self._json({"ok": True, "data": data})

    def _fail(self, message: str, code: int = 400) -> None:
        self._json({"ok": False, "error": str(message)}, code=code)

    # ------------------------------------------------------------ 安全 ---
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")

    def _token_ok(self) -> bool:
        token = self.headers.get("X-Bili-Token") or ""
        if not token:
            query = urllib.parse.urlparse(self.path).query
            token = urllib.parse.parse_qs(query).get("token", [""])[0]
        return secrets.compare_digest(token, self.app.token)

    # ------------------------------------------------------------ 路由 ---
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._fail("非法 Host", 403)
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            if not self._token_ok():
                return self._fail("缺少或错误的访问令牌", 401)
            return self._api_get(path, urllib.parse.parse_qs(parsed.query))
        return self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._fail("非法 Host", 403)
        if not self._token_ok():
            return self._fail("缺少或错误的访问令牌", 401)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            return self._fail(f"请求体不是合法 JSON：{exc}")
        path = urllib.parse.urlparse(self.path).path
        try:
            return self._api_post(path, payload if isinstance(payload, dict) else {})
        except PermissionError as exc:
            return self._fail(exc, 403)
        except FileNotFoundError as exc:
            return self._fail(exc, 404)
        except (BiliError, RiskControlError) as exc:
            return self._fail(exc if isinstance(exc, RiskControlError) else exc.message, 400)
        except ValueError as exc:
            return self._fail(exc, 400)
        except Exception as exc:  # noqa: BLE001
            error(f"处理 {path} 出错：{exc}")
            return self._fail(f"{type(exc).__name__}: {exc}", 500)

    # ------------------------------------------------------------ GET ---
    def _api_get(self, path: str, query: Dict[str, List[str]]) -> None:
        app = self.app
        if path == "/api/state":
            return self._ok(app.state())
        if path == "/api/jobs":
            with_logs = query.get("logs", ["0"])[0] in ("1", "true")
            return self._ok(app.jobs.list(with_logs=with_logs))
        if path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/"):].strip("/")
            job = app.jobs.get(job_id)
            if job is None:
                return self._fail("任务不存在", 404)
            since = int(query.get("since", ["0"])[0] or 0)
            return self._ok(job.to_dict(since=since))
        if path == "/api/history":
            return self._ok(app.jobs.history())
        return self._fail("未知接口", 404)

    # ------------------------------------------------------------ POST ---
    def _api_post(self, path: str, payload: Dict[str, Any]) -> None:
        app = self.app
        if path == "/api/parse":
            return self._ok(app.parse(str(payload.get("input", ""))))
        if path == "/api/jobs":
            text = str(payload.get("input", "")).strip()
            if not text:
                raise ValueError("请输入视频链接、BV号或搜索关键字")
            options = payload.get("options") or {}
            if isinstance(options, dict) and options.get("output_dir"):
                app.save_settings({"output_dir": options["output_dir"]})
            return self._ok(app.create_job(text, options if isinstance(options, dict) else {}))
        if path.startswith("/api/jobs/"):
            rest = path[len("/api/jobs/"):].strip("/")
            job_id, _, action = rest.partition("/")
            if action == "cancel":
                if not app.jobs.cancel(job_id):
                    return self._fail("任务不存在或已结束", 400)
                return self._ok(True)
            if action == "remove":
                if not app.jobs.remove(job_id):
                    return self._fail("任务不存在或仍在运行", 400)
                return self._ok(True)
            return self._fail("未知操作", 404)
        if path == "/api/settings":
            return self._ok(app.save_settings(payload))
        if path == "/api/reveal":
            app.reveal(str(payload.get("path", "")))
            return self._ok(True)
        if path == "/api/history/clear":
            return self._ok({"removed": app.jobs.clear_history()})
        return self._fail("未知接口", 404)

    # ---------------------------------------------------------- 静态文件 ---
    def _static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        rel = urllib.parse.unquote(path).lstrip("/")
        target = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(os.path.realpath(STATIC_DIR)):
            return self._fail("非法路径", 403)
        if not os.path.isfile(target):
            return self._fail("文件不存在", 404)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            return self._fail(f"读取失败：{exc}", 500)
        self._send(200, body, ctype)


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(app: App, host: str = "127.0.0.1", port: int = 8765,
                  max_tries: int = 20) -> WebServer:
    """创建 HTTP 服务；端口被占用时自动顺延。"""
    handler = type("BoundHandler", (Handler,), {"app": app})
    last_exc: Optional[Exception] = None
    for offset in range(max_tries):
        try:
            return WebServer((host, port + offset), handler)
        except OSError as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"端口 {port}~{port + max_tries} 都被占用：{last_exc}")


def run_web(client: HttpClient, api: BiliAPI, host: str = "127.0.0.1",
            port: int = 8765, data_dir: str = "", open_browser: bool = True,
            max_workers: int = 2, output_dir: str = "") -> int:
    """启动 Web 应用，阻塞直到 Ctrl-C。"""
    if not data_dir:
        data_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "..", "web_data")
        data_dir = os.path.abspath(data_dir)
    app = App(client, api, data_dir, max_workers=max_workers)
    if output_dir:  # 命令行显式 -o 时以它为准
        app.save_settings({"output_dir": os.path.abspath(os.path.expanduser(output_dir))})
    server = create_server(app, host, port)
    real_host, real_port = server.server_address[0], server.server_address[1]
    url = f"http://{real_host}:{real_port}/?token={app.token}"

    def say(line: str = "") -> None:
        print(line, flush=True)

    say()
    ok("网页界面已启动")
    say(f"  打开地址：{url}")
    say(f"  输出目录：{app.settings['output_dir']}")
    say(f"  ffmpeg  ：{app.ffmpeg or '未找到'}")
    say(f"  下载任务并发数：{max_workers}")
    say("  按 Ctrl-C 退出")
    say()

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        info("正在关闭…")
    finally:
        server.shutdown()
        server.server_close()
        app.jobs._pool.shutdown(wait=False)  # noqa: SLF001
    ok("已退出")
    return 0
