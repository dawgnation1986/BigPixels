#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""看看每个权重到底吃什么尺寸、吐什么尺寸。改引擎前先跑它。

薄壳：真正的实现在 app.tools.inspect_models。
壳在这里是为了 README 和文档里的老命令照旧能用：`python tools/inspect_models.py …`；
等价于直接 `python -m app.tools.inspect_models …`（不想用壳就删掉这个文件）。
"""
import os
import sys

# 这个文件躺在 tools/ 里，项目根在它上一层 —— 得把根挂上，才 import 得到 app.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tools.inspect_models import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
