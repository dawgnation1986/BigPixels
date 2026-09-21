#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""项目里所有“东西放哪儿”的唯一出处。

core 下的模块都从这里拿路径，谁也别自己数 dirname 的层数 ——
文件一搬位置，散落各处的 dirname 就会各错各的。
"""
from __future__ import annotations

import os

# app/core/paths.py -> app/core -> app -> 项目根
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODEL_DIR = os.path.join(ROOT, "models")
DEVICE_CACHE = os.path.join(MODEL_DIR, ".device_probe.json")
OUTPUT_DIR = os.path.join(ROOT, "outputs")
WEB_DIR = os.path.join(ROOT, "web")          # 前端静态资源（html/css/js/fonts/demo）
SAMPLE_DIR = os.path.join(ROOT, "samples")
CACHE_DIR = os.path.join(ROOT, ".cache")     # 临时/缓存一律压在这里，不碰 C 盘
