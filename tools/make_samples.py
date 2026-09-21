#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重新生成 samples/ 里那几张测试图样。

薄壳：真正的实现在 app.tools.make_samples。
壳在这里是为了 README 和文档里的老命令照旧能用：`python tools/make_samples.py …`；
等价于直接 `python -m app.tools.make_samples …`（不想用壳就删掉这个文件）。
"""
import os
import sys

# 这个文件躺在 tools/ 里，项目根在它上一层 —— 得把根挂上，才 import 得到 app.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tools.make_samples import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
