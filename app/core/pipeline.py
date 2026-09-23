#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端：串联多次网络推理 + 收尾缩放 + 收尾细节补偿（带通 / USM）。"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .imaging import brighten, declip, detail_band, unsharp
from .presets import plan_runs, resolve_bright, resolve_finish, resolve_model
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
            on_stage=None, runner: SRRunner | None = None,
            clean: bool = True) -> np.ndarray:
    runner = runner or build_runner(preset, scale, denoise, device, tile, overlap)
    runs, net_scale = plan_runs(preset, scale, runner.scale)
    passes = len(runs)
    # 进网络之前先把源图的 JPEG 振铃压掉：细节模型会把那几个灰阶的振铃当成纹理，
    # 放大成一片网纹（眼睛、嘴这种「小尺寸 + 四周全是强边」的地方最明显）。
    cur = declip(rgb) if clean else rgb
    for p, post_down in enumerate(runs):
        if on_stage:
            on_stage(f"第 {p + 1}/{passes} 次网络推理 · {runner.name}", p, passes)
        cur = runner.upscale(cur, post_down=post_down)
    if net_scale != scale:
        h, w = cur.shape[0] * scale // net_scale, cur.shape[1] * scale // net_scale
        cur = np.asarray(Image.fromarray((cur * 255 + 0.5).astype(np.uint8))
                         .resize((w, h), Image.LANCZOS), np.float32) / 255.0
    kind, r1, r2, gain = resolve_finish(preset, clear, scale)
    if gain > 0 and scale > 1:
        if on_stage:
            shown = f"带通 {r1:.2f}-{r2:.2f}px" if kind == "band" else f"半径 {r1:.2f}px"
            on_stage(f"收尾细节补偿 · {shown} 增益 {gain:.2f}", passes, passes)
        cur = detail_band(cur, r1, r2, gain) if kind == "band" else unsharp(cur, r1, gain)
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


