"""bili-dl —— 一个纯 Python 实现的 B 站视频下载器。

仅依赖 Python 标准库；可选依赖 ffmpeg 用于音视频合并。

如果项目目录下存在 vendor/（例如用
``pip install --target vendor imageio-ffmpeg`` 安装的静态 ffmpeg），
会自动加入模块搜索路径，实现「整个软件自带 ffmpeg、不污染系统环境」。
"""

import os as _os
import sys as _sys

__version__ = "1.0.0"

_VENDOR_DIR = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "vendor")
if _os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in _sys.path:
    _sys.path.insert(0, _VENDOR_DIR)

__all__ = ["__version__"]
