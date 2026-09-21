#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令行放大入口。用法跟以前完全一样。

薄壳：真正的实现在 app.cli.upscale。
留着这个文件是为了老的命令照旧能用（README 和双击启动脚本里都写着它）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.cli.upscale import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
