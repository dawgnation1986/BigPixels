#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BigPixels 放大的引擎内核。

外面（命令行 / 网页服务 / 自检脚本）统一 import 这个包，不直接碰下层的子模块 ——
这样内部再怎么拆，对外的名字不会变。

    from app.core import PRESETS, upscale, load_image, save_image
    import app.core as U                      # 老写法也照旧能用

模块分工：
    paths      项目里所有路径的唯一出处（ROOT / models / outputs / .cache）
    presets    预设表、降噪档、清晰度档，以及"档位 → 权重和锐化参数"的换算
    runner     单个 ONNX 模型的瓦片式推理器（尺寸自适配 / 羽化融合 / 后端数值自检）
    pipeline   端到端：串联多次网络 + 收尾缩放 + 收尾锐化
    imaging    读图 / 存图 / unsharp
    metrics    PSNR / SSIM —— 全项目只此一份，离线和网页端共用
"""
from __future__ import annotations

from .imaging import (                    # noqa: F401
    _gauss_blur, _gauss_kernel, load_image, save_image, unsharp,
)
from .paths import (                      # noqa: F401
    CACHE_DIR, DEVICE_CACHE, MODEL_DIR, OUTPUT_DIR, ROOT, SAMPLE_DIR, WEB_DIR,
)
from .pipeline import build_runner, upscale, upscale_alpha   # noqa: F401
from .presets import (                    # noqa: F401
    CLEAR_LABELS, CLEAR_LEVELS, DENOISE_LEVELS, PRESETS,
    plan_passes, resolve_model, resolve_sharpen,
)
from .runner import MIN_TILE, TILE_MULT, SRRunner, _is_nhwc, ort   # noqa: F401

__all__ = [
    "ROOT", "MODEL_DIR", "DEVICE_CACHE", "OUTPUT_DIR", "WEB_DIR", "SAMPLE_DIR", "CACHE_DIR",
    "PRESETS", "DENOISE_LEVELS", "CLEAR_LEVELS", "CLEAR_LABELS",
    "resolve_model", "plan_passes", "resolve_sharpen",
    "load_image", "save_image", "unsharp", "SRRunner", "ort",
    "build_runner", "upscale", "upscale_alpha",
]
