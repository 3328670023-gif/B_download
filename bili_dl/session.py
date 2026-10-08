"""HTTP 会话层：Cookie 管理、自动重试、JSON 解析、Range 分片下载。"""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import os
import random
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any, Dict, Iterable, Optional

from . import __version__
from .utils import debug, human_size, retry, warn

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

API_HOST = "https://api.bilibili.com"


class RiskControlError(RuntimeError):
    """触发 B 站风控（HTTP 412 / -352），通常需要登录 Cookie。"""

    def __init__(self, message: str = "", url: str = ""):
        self.url = url
        super().__init__(message or "触发 B 站风控（HTTP 412）")


class BiliError(RuntimeError):
    """B 站接口返回的业务错误。"""

    def __init__(self, code: int, message: str, url: str = ""):
        self.code = code
        self.message = message
        self.url = url
        super().__init__(f"接口错误 code={code} message={message}")


class HttpClient:
    """带 Cookie、UA、Referer 与重试的 urllib 包装。"""

    def __init__(self, cookie: str = "", user_agent: str = DEFAULT_UA,
                 timeout: float = 30.0, proxy: str = ""):
        self.user_agent = user_agent
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        handlers: list[Any] = [urllib.request.HTTPCookieProcessor(self.jar)]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        ctx = ssl.create_default_context()
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self._warmed = False
        if cookie:
            self.set_cookie_string(cookie)

    # ------------------------------------------------------------ Cookie ---
    def set_cookie_string(self, cookie: str) -> None:
        """解析 "SESSDATA=xxx; bili_jct=yyy" 形式的 Cookie 字符串。"""
        for part in re.split(r"[;\n]", cookie):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            self.set_cookie(name.strip(), value.strip())

    def set_cookie(self, name: str, value: str, domain: str = ".bilibili.com") -> None:
        c = http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
            path="/", path_specified=True, secure=False, expires=None,
            discard=False, comment=None, comment_url=None, rest={},
        )
        self.jar.set_cookie(c)

    def has_cookie(self, name: str) -> bool:
        return any(c.name == name and c.value for c in self.jar)

    def cookie_value(self, name: str) -> str:
        for c in self.jar:
            if c.name == name:
                return c.value
        return ""

    def cookie_header(self, url: str = "https://www.bilibili.com") -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar if c.value)

    # --------------------------------------------------------------- 请求 ---
    def build_request(self, url: str, headers: Optional[Dict[str, str]] = None,
                      method: str = "GET") -> urllib.request.Request:
        h = {
            "User-Agent": self.user_agent,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        }
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, headers=h, method=method)
        return req

    def open(self, url: str, headers: Optional[Dict[str, str]] = None,
             timeout: Optional[float] = None):
        req = self.build_request(url, headers)
        return self.opener.open(req, timeout=timeout or self.timeout)

    def get_bytes(self, url: str, headers: Optional[Dict[str, str]] = None,
                  timeout: Optional[float] = None, retries: int = 3) -> bytes:
        """下载并返回全部字节（自动处理 gzip/deflate）。"""
        last: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                with self.open(url, headers, timeout) as resp:
                    data = resp.read()
                    enc = (resp.headers.get("Content-Encoding") or "").lower()
                    if enc == "gzip":
                        data = gzip.decompress(data)
                    elif enc == "deflate":
                        try:
                            data = zlib.decompress(data)
                        except zlib.error:
                            data = zlib.decompress(data, -zlib.MAX_WBITS)
                    return data
            except urllib.error.HTTPError as exc:
                last = exc
                # 4xx（除 429）一般不值得重试
                if exc.code in (304, 400, 401, 403, 404) and attempt >= 2:
                    break
                if exc.code in (412, 429):
                    self.warm_up()
            except (urllib.error.URLError, socket.timeout, ConnectionError,
                    ssl.SSLError, TimeoutError) as exc:
                last = exc
            if attempt < retries:
                time.sleep(0.8 * attempt + random.random())
        if isinstance(last, urllib.error.HTTPError) and last.code == 412:
            raise RiskControlError(
                "触发 B 站风控（HTTP 412）。该接口通常需要登录态，"
                "请用 --cookie \"SESSDATA=...\" 传入浏览器 Cookie 后重试。",
                url)
        raise last if last else RuntimeError("请求失败")

    def get_json(self, url: str, headers: Optional[Dict[str, str]] = None,
                 retries: int = 3) -> Dict[str, Any]:
        raw = self.get_bytes(url, headers, retries=retries)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"接口返回非法 JSON：{raw[:200]!r}") from exc

    def api(self, path: str, params: Optional[Dict[str, Any]] = None,
            wbi: str = "", retries: int = 3) -> Dict[str, Any]:
        """调用 api.bilibili.com 上的接口；wbi 为 mixin_key 时自动签名。

        返回 data 字段；业务码非 0 时抛 BiliError。
        """
        url = path if path.startswith("http") else API_HOST + path
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if wbi:
            from .wbi import sign_url
            url = sign_url(url, params, wbi)
        else:
            if params:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = self.get_json(url, retries=retries)
        code = data.get("code", 0)
        if code != 0:
            raise BiliError(code, data.get("message", ""), url)
        return data.get("data") or {}

    # ------------------------------------------------------------- 预热 ---
    def warm_up(self) -> None:
        """访问首页 + 指纹接口，拿到 buvid3/buvid4 等风控 Cookie。

        没有 buvid3 时 space/wbi/arc/search 之类的接口会返回 -352。
        """
        try:
            if not self.has_cookie("buvid3"):
                with self.open("https://www.bilibili.com/") as resp:
                    resp.read(2048)
            spi = self.get_json(f"{API_HOST}/x/frontend/finger/spi", retries=2)
            d = spi.get("data") or {}
            if d.get("b_3"):
                self.set_cookie("buvid3", d["b_3"])
            if d.get("b_4"):
                self.set_cookie("buvid4", d["b_4"])
            self.set_cookie("b_nut", str(int(time.time())))
            self.set_cookie("buvid_fp", self.cookie_value("buvid3") or "unknown")
            if not self.has_cookie("buvid3"):
                # 兜底：本地随机生成一个合法格式的 buvid3
                self.set_cookie("buvid3", f"{_random_hex32()}-{int(time.time())}infoc")
            self._warmed = True
            debug(f"风控 Cookie 就绪：buvid3={self.cookie_value('buvid3')[:16]}…")
        except Exception as exc:  # 预热失败不应阻断主流程
            debug(f"预热失败：{exc}")

    def ensure_warm(self) -> None:
        if not self._warmed:
            self.warm_up()


def _random_hex32() -> str:
    return "".join(random.choice("0123456789ABCDEF") for _ in range(32))


# ------------------------------------------------------------ 分片下载 ---

class RangeNotSupported(RuntimeError):
    pass


def probe_size(client: HttpClient, url: str, headers: Optional[Dict[str, str]] = None,
               fallback: int = 0) -> int:
    """探测资源大小：优先 Content-Range，其次 Content-Length。"""
    h = dict(headers or {})
    h["Range"] = "bytes=0-0"
    try:
        with client.open(url, h) as resp:
            cr = resp.headers.get("Content-Range")
            resp.read(1)
        if cr and "/" in cr:
            total = cr.rsplit("/", 1)[-1].strip()
            if total.isdigit():
                return int(total)
        return fallback
    except urllib.error.HTTPError as exc:
        if exc.code == 416:  # 空文件
            return 0
        if exc.code in (200, 206):
            return fallback
        raise
    except Exception:
        return fallback


def download_range(client: HttpClient, url: str, start: int, end: int,
                   headers: Optional[Dict[str, str]] = None, retries: int = 4) -> bytes:
    """下载 [start, end] 闭区间字节。"""
    h = dict(headers or {})
    h["Range"] = f"bytes={start}-{end}"
    expected = end - start + 1
    last: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with client.open(url, h) as resp:
                data = b""
                while True:
                    block = resp.read(262144)
                    if not block:
                        break
                    data += block
            if len(data) < expected:
                raise IOError(f"分片长度不足：{len(data)} < {expected}")
            return data
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(0.5 * attempt + random.random())
    raise last if last else IOError("分片下载失败")
