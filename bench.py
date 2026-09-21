#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线画质基准：在自带样例上跑各预设，跟双三次基线比 PSNR / SSIM。

薄壳：真正的实现在 app.cli.bench。
留着这个文件是为了老的命令照旧能用（README 和双击启动脚本里都写着它）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.cli.bench import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
