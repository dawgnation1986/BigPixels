#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一次性核对 / 性能探针：
  1. 新的 baseline_patch（只插值中心块）与旧的「整幅插值再取中心」结果差多少
  2. 两者的耗时与峰值内存差多少

    .venv/Scripts/python.exe .cache/perf_probe.py [倍数]
"""
import os
import sys
import time
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web"))

import numpy as np                      # noqa: E402
from PIL import Image                   # noqa: E402
import server as S                      # noqa: E402
import metrics as M                     # noqa: E402

SCALE = int(sys.argv[1]) if len(sys.argv) > 1 else 4
SRC = os.path.join(ROOT, "web", "demo", "grain.png")
# 可选：先把源图拉到指定尺寸，用来复现真实照片那种规模
SRC_SIZE = None
if len(sys.argv) > 2 and "x" in sys.argv[2]:
    SRC_SIZE = tuple(int(x) for x in sys.argv[2].split("x"))


def old_way(src_rgb, w1, h1):
    """重构出来的旧写法：整幅插值，再取中心。"""
    base = (np.asarray(
        Image.fromarray((src_rgb * 255 + 0.5).astype(np.uint8))
        .resize((w1, h1), Image.BICUBIC), np.float32) / 255.0)
    return base


def run(label, fn):
    tracemalloc.start()
    t = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - t
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"  {label:<26} {dt:7.2f} s   峰值 {peak / 1048576:8.1f} MB")
    return out, dt, peak


def main():
    with Image.open(SRC) as im:
        src = im.convert("RGB")
    if SRC_SIZE:
        src = src.resize(SRC_SIZE, Image.LANCZOS)
    src_rgb = np.asarray(src, np.float32) / 255.0
    h0, w0 = src_rgb.shape[:2]
    w1, h1 = w0 * SCALE, h0 * SCALE
    print(f"输入 {w0}×{h0}  ·  输出 {w1}×{h1}  ·  {S.mp_of(w1, h1)} MP  ×{SCALE}\n")

    # 假的输出：用双三次凑出来就行，这里只比指标算法，不比模型
    out8 = np.asarray(src.resize((w1, h1), Image.BICUBIC).convert("RGB"))

    cw, ch = min(S.METRIC_CROP, w1), min(S.METRIC_CROP, h1)
    x0, y0 = (w1 - cw) // 2, (h1 - ch) // 2
    box = (x0, y0, x0 + cw, y0 + ch)

    base, t_old, m_old = run("旧：整幅双三次插值", lambda: old_way(src_rgb, w1, h1))
    bc_old = S.center_crop(base, S.METRIC_CROP)
    del base

    patch, t_new, m_new = run("新：只插值中心那一块",
                              lambda: S.baseline_patch(src_rgb, (w1, h1), box))

    print()
    if patch is None:
        print("  !! baseline_patch 返回 None，兜底逻辑被触发了")
        return 1
    d = np.abs(patch - bc_old)
    print(f"  中心块形状  旧 {bc_old.shape}  新 {patch.shape}")
    print(f"  最大绝对差  {d.max():.6f}   平均 {d.mean():.8f}")
    print(f"  锐度方差    旧 {M.sharpness(bc_old):.6f}   新 {M.sharpness(patch):.6f}")
    gain_old = M.sharpness(S.center_crop(np.asarray(Image.fromarray(out8), np.float32) / 255.0,
                                         S.METRIC_CROP)) / M.sharpness(bc_old)
    gain_new = M.sharpness(S.center_crop(np.asarray(Image.fromarray(out8), np.float32) / 255.0,
                                         S.METRIC_CROP)) / M.sharpness(patch)
    print(f"  锐度增益    旧 ×{gain_old:.4f}   新 ×{gain_new:.4f}   "
          f"（界面上保留两位，{'一致' if round(gain_old, 2) == round(gain_new, 2) else '不一致 !'}）")
    print(f"\n  耗时 {t_old / max(t_new, 1e-6):.1f}×  峰值内存 {m_old / max(m_new, 1):.0f}×")
    return 0


if __name__ == "__main__":
    sys.exit(main())
