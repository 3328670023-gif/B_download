"""支持 `python3 -m bili_dl` 方式运行。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
