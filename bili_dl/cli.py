"""bili-dl 命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from .api import QUALITY_ALIASES, QUALITY_NAMES, BiliAPI, quality_label
from .downloader import Downloader, Options, find_ffmpeg
from .parser import parse_target
from .runner import run_target
from .session import BiliError, HttpClient, RiskControlError
from .utils import (bold, blue, debug, dim, ensure_dir, error, green, info, ok,
                    red, short_path, warn, yellow)

BANNER = r"""
   _     _ _ _        _ _
  | |__ (_) (_)      | | |
  | '_ \| | | |  ____| | |     B 站视频下载器  v{ver}
  | |_) | | | | |____| | |     仅用 Python 标准库实现
  |_.__/|_|_|_|      |_|_|
"""


# ------------------------------------------------------------ 参数解析 ---

def parse_quality(value: str) -> int:
    v = str(value).strip().lower()
    if v in QUALITY_ALIASES:
        return QUALITY_ALIASES[v]
    v2 = v.replace("p", "").replace("P", "")
    if v2.isdigit():
        n = int(v2)
        if n in QUALITY_NAMES:
            return n
        # 允许直接写 1080 / 720 / 480 这类数字
        guess = {4320: 127, 2160: 120, 1440: 116, 1080: 80, 720: 64, 480: 32, 360: 16, 240: 6}
        if n in guess:
            return guess[n]
    raise argparse.ArgumentTypeError(
        f"无法识别的清晰度：{value}（可用：best/8k/4k/1080p/720p/480p/360p 或 qn 值）")


def parse_pages(value: str) -> Optional[List[int]]:
    """解析 "1,3-5" 形式的分P选择。"""
    if not value:
        return None
    out: List[int] = []
    for chunk in str(value).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                raise argparse.ArgumentTypeError(f"无法识别的分P范围：{chunk}")
            out.extend(range(lo, hi + 1))
        else:
            try:
                out.append(int(chunk))
            except ValueError:
                raise argparse.ArgumentTypeError(f"无法识别的分P：{chunk}")
    return sorted(set(out)) or None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bili-dl",
        description="B 站视频下载器：支持单个视频、分P、番剧、收藏夹、UP主投稿、合集、搜索。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  bili-dl BV1GJ411x7h7
  bili-dl https://www.bilibili.com/video/BV1GJ411x7h7?p=2 -q 1080p
  bili-dl BV1xx411c7mD -p 1,3-5 --cover --danmaku
  bili-dl "https://space.bilibili.com/946974/video" --max-items 20
  bili-dl "https://space.bilibili.com/946974/favlist?fid=123456" -o ~/影片
  bili-dl --search "机器学习 入门" --max-items 10
  bili-dl --cookie "SESSDATA=xxxx" BV1xx411c7mD -q 1080p+

  # 启动网页界面（推荐，双击「启动下载器.command」也可以）
  bili-dl --web
  bili-dl --web --port 9000 --cookie "SESSDATA=xxxx"
""")
    p.add_argument("targets", nargs="*", metavar="目标",
                   help="视频链接 / BV号 / av号 / 番剧链接 / 收藏夹 / UP主空间 / 合集 / 搜索词")
    p.add_argument("-o", "--output", default="", help="输出目录（默认 ./downloads）")
    p.add_argument("-q", "--quality", default="best", type=parse_quality,
                   help="清晰度：best/8k/4k/1080p+/1080p/720p/480p/360p（默认 best）")
    p.add_argument("--codec", default="avc", choices=["avc", "hevc", "av1", "best"],
                   help="视频编码偏好，avc 兼容性最好（默认 avc）")
    p.add_argument("-p", "--pages", default="", type=parse_pages,
                   help="只下载指定分P，如 1,3-5")
    p.add_argument("-j", "--workers", type=int, default=8,
                   help="单个文件的下载线程数（默认 8）")
    p.add_argument("--chunk-size", type=int, default=8,
                   help="分片大小，单位 MB（默认 8）")
    p.add_argument("--audio-only", action="store_true", help="只下载音频（m4a）")
    p.add_argument("--video-only", action="store_true", help="只下载视频画面（无声音）")
    p.add_argument("--format", default="auto", choices=["auto", "dash", "mp4"],
                   help="auto=有 ffmpeg 就用 DASH，否则用 MP4 合流；dash=强制分离流；mp4=强制合流")
    p.add_argument("--cover", action="store_true", help="同时保存封面")
    p.add_argument("--danmaku", action="store_true", help="同时保存弹幕 XML")
    p.add_argument("--subtitle", action="store_true", help="同时保存 CC 字幕（srt）")
    p.add_argument("--no-metadata", action="store_true", help="不写 .info.json")
    p.add_argument("--cookie", default="", help="Cookie 字符串，如 \"SESSDATA=xxx; bili_jct=yyy\"")
    p.add_argument("--cookie-file", default="", help="从文件读取 Cookie")
    p.add_argument("--config", default="", help="JSON 配置文件路径（默认找 ./config.json）")
    p.add_argument("--ffmpeg", default="", help="指定 ffmpeg 可执行文件路径")
    p.add_argument("--max-items", type=int, default=0,
                   help="批量下载（收藏夹/投稿/合集/搜索）时的最大条数，0 表示不限")
    p.add_argument("--list", default="", metavar="文件", help="从文本文件读取批量目标，每行一个")
    p.add_argument("--search", default="", metavar="关键字", help="直接按关键字搜索并下载")
    p.add_argument("--flat", action="store_true", help="批量下载时不按合集名建子目录")
    p.add_argument("--overwrite", action="store_true", help="覆盖已存在的文件")
    p.add_argument("--keep-temp", action="store_true", help="保留临时文件")
    p.add_argument("--dry-run", action="store_true", help="只显示清晰度与输出路径，不下载")
    p.add_argument("--web", action="store_true",
                   help="启动网页界面（浏览器里粘贴链接下载），默认端口 8765")
    p.add_argument("--port", type=int, default=8765, help="网页界面的端口（默认 8765）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认只允许本机 127.0.0.1）")
    p.add_argument("--no-browser", action="store_true", help="启动网页界面时不自动打开浏览器")
    p.add_argument("--web-concurrency", type=int, default=2,
                   help="网页界面同时下载的任务数（默认 2）")
    p.add_argument("--install-ffmpeg", action="store_true",
                   help="通过 pip 安装 imageio-ffmpeg（内含静态 ffmpeg，用于音视频合并）后退出")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试信息")
    p.add_argument("-V", "--version", action="version",
                   version=f"bili-dl {__version__}")
    return p


# ------------------------------------------------------------ 配置文件 ---

def load_config(path: str) -> Dict[str, Any]:
    candidates = [path] if path else []
    if not path:
        candidates = ["config.json",
                      os.path.expanduser("~/.config/bili-dl/config.json")]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                debug(f"已加载配置文件：{cand}")
                return data if isinstance(data, dict) else {}
            except Exception as exc:
                warn(f"配置文件解析失败 {cand}: {exc}")
    return {}


def config_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把配置文件里的值变成 argparse 的默认值（命令行参数依然优先）。"""
    out: Dict[str, Any] = {}
    for key in ("output", "quality", "codec", "workers", "cookie", "ffmpeg",
                "max_items", "format", "chunk_size"):
        if cfg.get(key) not in (None, ""):
            out[key] = cfg[key]
    for flag in ("cover", "danmaku", "subtitle", "flat", "overwrite", "keep_temp"):
        if cfg.get(flag):
            out[flag] = True
    return out


# ---------------------------------------------------------------- 主流程 ---

def build_options(args: argparse.Namespace) -> Options:
    return Options(
        output_dir=args.output or "downloads",
        quality=args.quality,
        codec=args.codec,
        pages=args.pages,
        workers=max(1, args.workers),
        chunk_size=max(1, args.chunk_size) * 1024 * 1024,
        audio_only=args.audio_only,
        video_only=args.video_only,
        format=args.format,
        cover=args.cover,
        danmaku=args.danmaku,
        subtitle=args.subtitle,
        metadata=not args.no_metadata,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        keep_temp=args.keep_temp,
        ffmpeg=args.ffmpeg,
        max_items=args.max_items,
        flat=args.flat,
    )


def resolve_cookie(args: argparse.Namespace, cfg: Dict[str, Any]) -> str:
    if args.cookie:
        return args.cookie
    if args.cookie_file and os.path.isfile(args.cookie_file):
        with open(args.cookie_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    if cfg.get("cookie"):
        return str(cfg["cookie"])
    for env in ("BILI_COOKIE", "BILIBILI_COOKIE", "SESSDATA"):
        if os.environ.get(env):
            val = os.environ[env]
            return val if "=" in val else f"SESSDATA={val}"
    return ""


def collect_targets(args: argparse.Namespace) -> List[str]:
    targets = list(args.targets)
    if args.search:
        targets.append(args.search)
    if args.list:
        if not os.path.isfile(args.list):
            raise FileNotFoundError(f"目标列表文件不存在：{args.list}")
        with open(args.list, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    targets.append(line)
    return targets


def main(argv: Optional[List[str]] = None) -> int:
    # 先探测 --config，再据此设置默认值，最后正式解析（命令行 > 配置文件 > 内置默认）
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default="")
    known, _ = pre.parse_known_args(argv)
    cfg = load_config(known.config)

    parser = build_parser()
    try:
        parser.set_defaults(**config_defaults(cfg))
    except Exception as exc:
        warn(f"配置文件中的部分字段被忽略：{exc}")
    args = parser.parse_args(argv)
    if args.verbose:
        os.environ["BILI_DL_DEBUG"] = "1"
    if isinstance(args.pages, str):
        args.pages = parse_pages(args.pages)

    try:
        targets = collect_targets(args)
    except FileNotFoundError as exc:
        error(str(exc))
        return 2
    if args.install_ffmpeg:
        return install_ffmpeg()

    if args.web:
        from .web.server import run_web
        client = HttpClient(cookie=resolve_cookie(args, cfg))
        client.warm_up()
        api = BiliAPI(client)
        return run_web(client, api, host=args.host, port=args.port,
                       open_browser=not args.no_browser,
                       max_workers=max(1, args.web_concurrency),
                       output_dir=args.output if args.output else "")

    if not targets:
        parser.print_help()
        return 0



    print(BANNER.format(ver=__version__))

    cookie = resolve_cookie(args, cfg)
    client = HttpClient(cookie=cookie)
    client.warm_up()
    api = BiliAPI(client)
    dl = Downloader(client, api, build_options(args))

    # 环境概览
    logged = api.is_login()
    who = ""
    if logged:
        me = api.self_info()
        who = me.get("uname", "")
    print(f"  登录状态：{green('已登录 ' + who) if logged else yellow('未登录（最高 720P，大会员画质不可用）')}")
    print(f"  ffmpeg  ：{green(dl.ffmpeg) if dl.ffmpeg else red('未找到（将使用 MP4 合流或保留原始流）')}")
    print(f"  输出目录：{os.path.abspath(dl.opts.output_dir)}")
    print(f"  清晰度  ：{quality_label(dl.opts.quality)}")
    print()

    interrupted = False
    try:
        for t in targets:
            try:
                # parse_target 会自动区分链接/ID/搜索词
                run_target(t, api, dl, client)
            except RiskControlError as exc:
                dl.stats["fail"] += 1
                error(f"{t} → {exc}")
            except BiliError as exc:
                dl.stats["fail"] += 1
                error(f"{t} → 接口错误：{exc.message} (code={exc.code})")
            except Exception as exc:
                dl.stats["fail"] += 1
                error(f"{t} → 失败：{exc}")
                if args.verbose:
                    import traceback
                    traceback.print_exc()
    except KeyboardInterrupt:
        interrupted = True
        print()
        warn("已中断（已下载的分片会保留，重新运行可断点续传）")

    print()
    s = dl.stats
    print(f"{bold('汇总')}：成功 {green(s['ok'])}  跳过 {dim(s['skip'])}  "
          f"失败 {red(s['fail']) if s['fail'] else '0'}")
    if interrupted:
        return 130
    return 0 if s["fail"] == 0 else 1


def install_ffmpeg() -> int:
    """通过 pip 安装 imageio-ffmpeg，得到一个自带的静态 ffmpeg。"""
    import subprocess
    info("正在安装 imageio-ffmpeg（内含静态 ffmpeg，约 25MB）…")
    cmds = [
        [sys.executable, "-m", "pip", "install", "--user", "imageio-ffmpeg"],
        [sys.executable, "-m", "pip", "install", "imageio-ffmpeg"],
    ]
    for cmd in cmds:
        try:
            rc = subprocess.call(cmd)
        except Exception as exc:
            error(f"执行失败：{exc}")
            return 1
        if rc == 0:
            exe = find_ffmpeg()
            if exe:
                ok(f"安装成功，ffmpeg：{exe}")
                return 0
            warn("安装完成但未能定位 ffmpeg，请检查 pip 输出")
            return 1
    error("安装失败，可改用：brew install ffmpeg")
    return 1


if __name__ == "__main__":
    sys.exit(main())
