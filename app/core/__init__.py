#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BigPixels 放大的引擎内核。

外面（命令行 / 网页服务 / 自检脚本）统一 import 这个包，不直接碰下层的子模块 ——
这样内部再怎么拆，对外的名字不会变。

    from app.core import PRESETS, upscale, load_image, save_image
    import app.core as U                      # 老写法也照旧能用

模块分工：
    paths      项目里所有路径的唯一出处（ROOT / models / outputs / .cache / locales）
    settings   用户设置（保存模式、界面语言）—— outputs/settings.json 只由它读写
    i18n       界面语言与词条查找，词条在 app/locales/<lang>.json
    presets    预设表、降噪档、清晰度档，以及"档位 → 权重和锐化参数"的换算
    runner     单个 ONNX 模型的瓦片式推理器（尺寸自适配 / 羽化融合 / 后端数值自检）
    pipeline   端到端：串联多次网络 + 收尾缩放 + 收尾锐化
    imaging    读图 / 存图 / unsharp
    metrics    PSNR / SSIM —— 全项目只此一份，离线和网页端共用

路径、设置、语言、预设是**立刻导入**的：它们只用标准库，而
`app/server/bootstrap.py` 与 `setup_env.py` 必须在「依赖还没装」的第一次运行里
就能拿到语言和路径。推理那几个模块要 numpy / PIL / onnxruntime，所以按需再导
—— 见下面的 __getattr__。
"""
from __future__ import annotations

from importlib import import_module

from .i18n import (                       # noqa: F401
    DEFAULT as DEFAULT_LANG, LANGS, NAMES as LANG_NAMES, current as lang_of, t, web_table,
)
from .paths import (                      # noqa: F401
    CACHE_DIR, DEVICE_CACHE, LOCALE_DIR, MODEL_DIR, OUTPUT_DIR, ROOT, SAMPLE_DIR,
    SETTINGS_PATH, WEB_DIR,
)
from .presets import (                    # noqa: F401
    BRIGHT_LEVELS, CLEAR_LEVELS, DENOISE_LEVELS, PRESETS, label,
    plan_passes, resolve_bright, resolve_model, resolve_sharpen,
)
from . import i18n, settings              # noqa: F401

# 名字 -> 定义它的子模块。只有真被用到时才把它们拉进来。
_LAZY: dict[str, str] = {}


def _lazy(mod: str, *names: str) -> None:
    for n in names:
        _LAZY[n] = mod


_lazy("imaging", "_gauss_blur", "_gauss_kernel", "brighten", "load_image", "save_image",
      "unsharp")
_lazy("pipeline", "build_runner", "upscale", "upscale_alpha")
_lazy("runner", "MIN_TILE", "TILE_MULT", "SRRunner", "_is_nhwc", "ort")


def __getattr__(name: str):
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("." + mod, __name__), name)
    globals()[name] = value               # 记住，别每次都绕这一圈
    return value


def __dir__():
    return sorted(list(globals()) + list(_LAZY))


__all__ = [
    "ROOT", "MODEL_DIR", "DEVICE_CACHE", "OUTPUT_DIR", "WEB_DIR", "SAMPLE_DIR", "CACHE_DIR",
    "LOCALE_DIR", "SETTINGS_PATH",
    "LANGS", "LANG_NAMES", "DEFAULT_LANG", "lang_of", "t", "web_table", "i18n", "settings",
    "PRESETS", "DENOISE_LEVELS", "CLEAR_LEVELS", "BRIGHT_LEVELS", "label",
    "resolve_model", "plan_passes", "resolve_sharpen", "resolve_bright",
    "load_image", "save_image", "unsharp", "brighten", "SRRunner", "ort",
    "build_runner", "upscale", "upscale_alpha",
]
