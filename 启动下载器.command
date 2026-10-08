#!/bin/bash
# ============================================================
#  双击本文件即可启动 B站下载器网页界面
#  关闭这个终端窗口（或按 Control-C）即可退出程序
# ============================================================
cd "$(dirname "$0")" || exit 1

echo "正在查找 Python…"
PY=""
for p in /opt/homebrew/bin/python3 /opt/miniconda3/bin/python3 \
         /usr/local/bin/python3 /usr/bin/python3; do
  if [ -x "$p" ]; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then PY="$(command -v python3 2>/dev/null)"; fi
if [ -z "$PY" ]; then
  echo
  echo "❌ 没有找到 python3。"
  echo "   请先安装 Python 3.8 或更高版本：https://www.python.org/downloads/"
  echo
  read -r -p "按回车键关闭窗口…" _
  exit 1
fi

echo "使用：$PY ($("$PY" -V 2>&1))"
echo
exec "$PY" bili-dl.py --web "$@"
