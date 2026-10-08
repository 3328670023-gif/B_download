#!/usr/bin/env python3
"""bili-dl 启动脚本。

用法：
    python3 bili-dl.py BV1GJ411x7h7
    python3 bili-dl.py --help
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bili_dl.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
