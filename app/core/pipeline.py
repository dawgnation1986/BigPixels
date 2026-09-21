#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端：串联多次网络推理 + 收尾缩放 + 收尾锐化。"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .imaging import brighten, unsharp
from .presets import plan_passes, resolve_bright, resolve_model, resolve_sharpen
from .runner import SRRunner


# --------------------------------------------------------------------------- #
# 端到端：多次串联 + 收尾缩放
# --------------------------------------------------------------------------- #
def build_runner(preset: str, scale: int, denoise: str = "medium", device: str = "auto",
                 tile: int = 256, overlap: int = 16) -> SRRunner:
    return SRRunner(resolve_model(preset, denoise, scale), device=device,
                    tile=tile, overlap=overlap)


def upscale(rgb: np.ndarray, preset: str, scale: int, denoise: str = "medium",
            device: str = "auto", tile: int = 256, overlap: int = 16,
            clear: str | float = "normal", bright: str | float = "off",
            on_stage=None, runner: SRRunner | None = None) -> np.ndarray:
    runner = runner or build_runner(preset, scale, denoise, device, tile, overlap)
    passes, net_scale = plan_passes(scale, runner.scale)
    cur = rgb
    for p in range(passes):
        if on_stage:
            on_stage(f"第 {p + 1}/{passes} 次网络推理 · {runner.name}", p, passes)
        cur = runner.upscale(cur)
    if net_scale != scale:
        h, w = cur.shape[0] * scale // net_scale, cur.shape[1] * scale // net_scale
        cur = np.asarray(Image.fromarray((cur * 255 + 0.5).astype(np.uint8))
                         .resize((w, h), Image.LANCZOS), np.float32) / 255.0
    radius, gain = resolve_sharpen(preset, clear)
    if gain > 0 and scale > 1:
        if on_stage:
            on_stage(f"收尾锐化 · 半径 {radius}px 增益 {gain:.2f}", passes, passes)
        cur = unsharp(cur, radius, gain)
    # 明暗开关放在最后：它是纯观感调整，不该影响上面任何一步的判断。
    k = resolve_bright(bright)
    if abs(k - 1.0) > 1e-9:
        if on_stage:
            on_stage(f"明暗 · 整体 ×{k:.3f}", passes, passes)
        cur = brighten(cur, k)
    return cur


def upscale_alpha(alpha: np.ndarray | None, scale: int) -> np.ndarray | None:
    if alpha is None:
        return None
    h, w = alpha.shape[0] * scale, alpha.shape[1] * scale
    im = Image.fromarray((np.clip(alpha, 0, 1) * 255 + 0.5).astype(np.uint8), mode="L")
    return np.asarray(im.resize((w, h), Image.LANCZOS), np.float32) / 255.0


