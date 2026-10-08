"""弹幕下载：把 B 站的分段 protobuf 弹幕转换成通用 XML 格式。

B 站现在的弹幕接口是 protobuf（/x/v2/dm/web/seg.so），这里用一个极简的
protobuf 解码器把它读成 Python 结构，再生成播放器可识别的 XML。
"""

from __future__ import annotations

import html
import urllib.error
import urllib.parse
from typing import Any, Dict, List, Tuple

from .session import HttpClient
from .utils import debug

DM_SEG_URL = "https://api.bilibili.com/x/v2/dm/web/seg.so"
DM_XML_URL = "https://comment.bilibili.com/{cid}.xml"

# 弹幕模式：1/2/3 滚动，4 底部，5 顶部，6 逆向，7 高级，8 代码，9 BAS
_MODE_NAMES = {1: "滚动", 4: "底部", 5: "顶部"}


# ------------------------------------------------------- 极简 protobuf ---

def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("varint 越界")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def _iter_fields(buf: bytes, pos: int = 0, end: int | None = None):
    """迭代 protobuf 顶层字段，yield (field_no, wire_type, value)。"""
    end = len(buf) if end is None else end
    while pos < end:
        key, pos = _read_varint(buf, pos)
        field_no, wire = key >> 3, key & 0x7
        if wire == 0:      # varint
            value, pos = _read_varint(buf, pos)
        elif wire == 1:    # 64-bit
            value, pos = buf[pos:pos + 8], pos + 8
        elif wire == 2:    # length-delimited
            length, pos = _read_varint(buf, pos)
            value, pos = buf[pos:pos + length], pos + length
        elif wire == 5:    # 32-bit
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            raise ValueError(f"不支持的 wire type {wire}")
        yield field_no, wire, value


def parse_danmaku_seg(buf: bytes) -> List[Dict[str, Any]]:
    """解析 DmSegMobileReply，返回弹幕字典列表。"""
    out: List[Dict[str, Any]] = []
    for field_no, wire, value in _iter_fields(buf):
        if field_no != 1 or wire != 2 or not isinstance(value, bytes):
            continue
        elem: Dict[str, Any] = {}
        for fno, fwire, fval in _iter_fields(value):
            if fwire == 2:
                try:
                    elem[fno] = fval.decode("utf-8")
                except UnicodeDecodeError:
                    elem[fno] = ""
            else:
                elem[fno] = fval
        out.append({
            "id": elem.get(1, 0),
            "progress": elem.get(2, 0),          # 毫秒
            "mode": elem.get(3, 1),
            "fontsize": elem.get(4, 25),
            "color": elem.get(5, 16777215),
            "mid_hash": elem.get(6, ""),
            "content": elem.get(7, ""),
            "ctime": elem.get(8, 0),
            "pool": elem.get(11, 0),
            "id_str": elem.get(12, ""),
        })
    return out


# ---------------------------------------------------------------- 下载 ---

def fetch_danmaku(client: HttpClient, cid: int, max_segments: int = 200) -> List[Dict[str, Any]]:
    """抓取某个 cid 的全部弹幕（分段 protobuf，失败则回退 XML）。"""
    all_items: List[Dict[str, Any]] = []
    seg = 1
    while seg <= max_segments:
        url = f"{DM_SEG_URL}?{urllib.parse.urlencode({'type': 1, 'oid': cid, 'segment_index': seg})}"
        try:
            raw = client.get_bytes(url, retries=2)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                # B 站对「没有弹幕的分段」返回 304，属于正常结束
                debug(f"弹幕分段 {seg} 无内容（304），结束抓取")
            else:
                debug(f"弹幕分段 {seg} 下载失败：{exc}")
            break
        except Exception as exc:
            debug(f"弹幕分段 {seg} 下载失败：{exc}")
            break
        if not raw:
            break
        try:
            items = parse_danmaku_seg(raw)
        except Exception as exc:
            debug(f"弹幕分段 {seg} 解析失败：{exc}")
            break
        if not items:
            break
        all_items.extend(items)
        seg += 1
    if not all_items:
        all_items = _fetch_danmaku_xml(client, cid)
    return all_items


def _fetch_danmaku_xml(client: HttpClient, cid: int) -> List[Dict[str, Any]]:
    """老版 XML 接口兜底。"""
    import re
    try:
        raw = client.get_bytes(DM_XML_URL.format(cid=cid), retries=2)
    except Exception as exc:
        debug(f"XML 弹幕下载失败：{exc}")
        return []
    text = raw.decode("utf-8", "replace")
    items = []
    for m in re.finditer(r'<d p="([^"]*)">(.*?)</d>', text, re.S):
        parts = m.group(1).split(",")
        if len(parts) < 8:
            continue
        try:
            items.append({
                "progress": int(float(parts[0]) * 1000),
                "mode": int(parts[1]),
                "fontsize": int(parts[2]),
                "color": int(parts[3]),
                "ctime": int(parts[4]),
                "pool": int(parts[5]),
                "mid_hash": parts[6],
                "id_str": parts[7],
                "id": int(parts[7]) if parts[7].isdigit() else 0,
                "content": html.unescape(m.group(2)),
            })
        except (ValueError, IndexError):
            continue
    return items


def to_xml(items: List[Dict[str, Any]], cid: int = 0, chat_id: int = 0) -> str:
    """把弹幕列表渲染成 B 站兼容的 XML。"""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<i>",
        f"  <chatserver>chat.bilibili.com</chatserver>",
        f"  <chatid>{chat_id or cid}</chatid>",
        "  <mission>0</mission>",
        "  <maxlimit>99999</maxlimit>",
        "  <state>0</state>",
        "  <real_name>0</real_name>",
        "  <source>k-v</source>",
    ]
    for it in sorted(items, key=lambda x: x.get("progress", 0)):
        p = ",".join([
            f"{it.get('progress', 0) / 1000:.3f}",
            str(it.get("mode", 1)),
            str(it.get("fontsize", 25)),
            str(it.get("color", 16777215)),
            str(it.get("ctime", 0)),
            str(it.get("pool", 0)),
            str(it.get("mid_hash", "")),
            str(it.get("id_str") or it.get("id", 0)),
        ])
        content = html.escape(str(it.get("content", "")), quote=True)
        lines.append(f'  <d p="{p}">{content}</d>')
    lines.append("</i>")
    return "\n".join(lines)
