#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重新生成 samples/ 里那几张测试图样。

薄壳：真正的实现在 app.tools.make_samples。
留着这个文件是为了老的命令照旧能用（README 和双击启动脚本里都写着它）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.tools.make_samples import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
