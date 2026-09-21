# -*- coding: utf-8 -*-
"""摸清每个 ONNX 模型的真实输入输出约定：直接 SR？还是"先插值、网络修残差"？"""
import os
import numpy as np
import onnxruntime as ort
from PIL import Image

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.core.paths import MODEL_DIR                                    # noqa: E402

TARGETS = [
    "waifu2x_cunet_art_noise2_2x.onnx",
    "waifu2x_cunet_art_dn1x.onnx",
    "waifu2x_swin_photo_noise1_2x.onnx",
    "RealESRGAN_x4plus.onnx",
    "4x-AnimeSharp.onnx",
]


def pattern(size: int) -> np.ndarray:
    """造一张有硬边缘 + 渐变的测试图"""
    a = np.zeros((32, 32, 3), np.float32)
    a[:, :16] = [0.15, 0.25, 0.35]
    a[:, 16:] = [0.80, 0.70, 0.60]
    a[8:24, 6:22] = [0.95, 0.30, 0.30]
    a[12:20, 10:18] = [0.10, 0.85, 0.40]
    im = Image.fromarray((a * 255).astype(np.uint8)).resize((size, size), Image.BICUBIC)
    return np.asarray(im, np.float32) / 255.0


def run(sess, x_hwc):
    i = sess.get_inputs()[0]
    shape = list(i.shape)
    nhwc = len(shape) == 4 and shape[-1] == 3 and shape[1] != 3
    x = np.transpose(x_hwc, (2, 0, 1))[None] if not nhwc else x_hwc[None]
    y = sess.run(None, {i.name: x.astype(np.float32)})[0]
    y = np.asarray(y, np.float32)
    onhwc = y.shape[-1] == 3 and y.shape[1] != 3
    yn = np.transpose(y, (0, 3, 1, 2)) if onhwc else y
    return yn[0], nhwc, onhwc


def main():
    so = ort.SessionOptions()
    so.log_severity_level = 3
    for f in TARGETS:
        p = os.path.join(MODEL_DIR, f)
        if not os.path.isfile(p):
            print(f"{f}: 缺失")
            continue
        print("=" * 78)
        print(f"{f}   {os.path.getsize(p)/1048576:.1f}MB")
        sess = ort.InferenceSession(p, sess_options=so, providers=["CPUExecutionProvider"])
        i, o = sess.get_inputs()[0], sess.get_outputs()[0]
        print(f"  in  {i.name:12s} {i.shape}  {i.type}")
        print(f"  out {o.name:12s} {o.shape}  {o.type}")

        fixed = isinstance(i.shape[2], int) and isinstance(i.shape[3], int)
        for size in ([int(i.shape[2])] if fixed else [32, 64]):
            try:
                src = pattern(size)
                y, nhwc, onhwc = run(sess, src)
                tgt = pattern(size * 2)
                print(f"  输入 {src.shape[1]}x{src.shape[0]} (nhwc={nhwc}) -> 输出 {y.shape[2]}x{y.shape[1]} "
                      f"(nhwc={onhwc}) 比例 {y.shape[1]/src.shape[0]:.2f}")
                print(f"     输出范围 [{y.min():.3f}, {y.max():.3f}] 均值 {y.mean():.3f}")
                if y.shape[1] == src.shape[0]:
                    d = y - np.transpose(src, (2, 0, 1))
                    print(f"     与输入之差：均值 {d.mean():+.4f}  绝对值最大 {np.abs(d).max():.4f}  "
                          f"→ {'残差型（需加回输入）' if abs(d.mean()) < 0.02 and np.abs(y).max() < 0.6 else '直接输出型'}")
                    up = np.transpose(np.asarray(
                        Image.fromarray((src * 255).astype(np.uint8)).resize(
                            (src.shape[1] * 2, src.shape[0] * 2), Image.BICUBIC), np.float32) / 255.0, (2, 0, 1))
                    print(f"     与“双三次放大 2x 后”对比：均值 {np.mean(y - up):+.4f}  "
                          f"绝对值最大 {np.abs(y - up).max():.4f}")
            except Exception as e:
                print(f"  输入 {size} 失败：{type(e).__name__}: {str(e)[:120]}")


if __name__ == "__main__":
    main()
