#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令行放大入口。用法跟以前完全一样。

薄壳：真正的实现在 app.cli.upscale。
壳在这里是为了 README 和文档里的老命令照旧能用：`python tools/upscale.py …`；
等价于直接 `python -m app.cli.upscale …`（不想用壳就删掉这个文件）。
"""
import os
import sys

# 这个文件躺在 tools/ 里，项目根在它上一层 —— 得把根挂上，才 import 得到 app.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cli.upscale import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
