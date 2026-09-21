#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""看看每个权重到底吃什么尺寸、吐什么尺寸。改引擎前先跑它。

薄壳：真正的实现在 app.tools.inspect_models。
留着这个文件是为了老的命令照旧能用（README 和双击启动脚本里都写着它）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.tools.inspect_models import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
