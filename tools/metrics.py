#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PSNR / SSIM / 锐度 —— 全项目只此一份，离线评测和网页端共用同一套实现。

壳：实现在 app/core/metrics.py。这里显式列出名字，别用 import * 图省事 ——
漏掉一个，调用方就得到"明明导进来了却 AttributeError"。
"""
import os
import sys

# 这个文件躺在 tools/ 里，项目根在它上一层 —— 得把根挂上，才 import 得到 app.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.metrics import LUMA, psnr, sharpness, ssim, to_luma  # noqa: E402,F401

__all__ = ["LUMA", "to_luma", "psnr", "ssim", "sharpness"]
