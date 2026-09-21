#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BigPixels 放大台 —— 基于深度卷积神经网络的图片放大（超分辨率）

原理与线上服务一致：
    低分图 --（必要时先插值放大）--> 深度卷积网络预测修正 --> 高清大图

权重换成开源版本，共两族：
    waifu2x（CUnet / Swin-UNet）—— 就是线上那个 bigjpg 用的那一族，art/photo 双域 + noise0~3 四档降噪
    Real-ESRGAN x4plus / 4x-AnimeSharp —— 现代 GAN 系，照片与动漫线稿更强

不同权重族的输入输出约定并不一样（waifu2x 会“吃掉”一圈边界像素，
Real-ESRGAN 则是直出 4x），所以本程序在加载模型时**自动探测**尺寸规律
out = scale * in + b，并据此决定外扩与裁切量，不写死任何魔数。

用法示例：
    python upscale.py in.jpg -o out.png --preset art --denoise medium --scale 4
    python upscale.py in.jpg -o out.png --clear crisp        # 线条更硬，接近线上那种观感
    python upscale.py D:\\pics -o D:\\out --preset photo --scale 2
    python upscale.py --list
    python upscale.py --info
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import numpy as np
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover
    sys.exit("缺少 onnxruntime，请先执行:  .\\.venv\\Scripts\\python.exe -m pip install onnxruntime-directml")

ROOT = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(ROOT, "models")
DEVICE_CACHE = os.path.join(MODEL_DIR, ".device_probe.json")

# 瓦片送入网络前先补齐到这个倍数，避免网络内部下采样出现非整除尺寸
TILE_MULT = 32
MIN_TILE = 64


def _is_nhwc(shape) -> bool:
    """判断 4 维张量布局是否为 NHWC（通道在最后）"""
    return len(shape) == 4 and shape[-1] == 3 and shape[1] != 3


# --------------------------------------------------------------------------- #
# 模型注册表
# --------------------------------------------------------------------------- #
# noise: 降噪程度档位。无/低/中/高/最高 -> 0/1/2/3/3
#        上游 waifu2x 只发布 noise0~3 四组权重，“最高”回落到 noise3。
#
# sharp: 收尾锐化的基准强度 (半径 px, 增益)。增益 = 该像素比高斯模糊版多出来的量乘几倍。
#   为什么需要它：网络出来的边缘天然是「渐变」的。waifu2x CUnet 尤其软 ——
#   4x 之后一条 1px 的线会摊成 4~7px 的灰带，细线整个糊掉。
#   实测（351×1295 的动漫插画 → 4x，拉普拉斯方差，越大越锐）：
#       双三次/Lanczos 基线 9.8 · art 原样 100 · art+锐化 872 · bigjpg 4x 卡通 914
#   锐化只在边缘起作用：平坦区 32×32 块的 std 始终 0.00，不会把干净的地方磨出噪点。
#   照片档给得保守（真实噪声会被一起放大），插画档给得足。
PRESETS: dict[str, dict] = {
    "art": {
        "label": "卡通 / 插画",
        "desc": "卡通、插画、线稿都能用，速度最快",
        "tech": "waifu2x CUnet · 原生 2× · 4.9 MB/档 · 带四档降噪权重",
        "noise": {
            0: "waifu2x_cunet_art_noise0_2x.onnx",
            1: "waifu2x_cunet_art_noise1_2x.onnx",
            2: "waifu2x_cunet_art_noise2_2x.onnx",
            3: "waifu2x_cunet_art_noise3_2x.onnx",
        },
        "dn_only": "waifu2x_cunet_art_dn1x.onnx",
        "hint": 2,
        "sharp": (1.3, 2.0),
    },
    "art-hd": {
        "label": "插画 高清",
        "desc": "插画专用，比上一档更干净，也更慢",
        "tech": "waifu2x Swin-UNet · 原生 2× · 16 MB/档 · 带四档降噪权重",
        "noise": {
            0: "waifu2x_swin_art_noise0_2x.onnx",
            1: "waifu2x_swin_art_noise1_2x.onnx",
            2: "waifu2x_swin_art_noise2_2x.onnx",
            3: "waifu2x_swin_art_noise3_2x.onnx",
        },
        "dn_only": None,
        "hint": 2,
        "sharp": (1.1, 1.6),
    },
    "photo": {
        "label": "照片 / 实拍",
        "desc": "真实照片，输出偏保守",
        "tech": "waifu2x Swin-UNet photo 域 · 原生 2× · 18 MB/档",
        "noise": {
            0: "waifu2x_swin_photo_noise0_2x.onnx",
            1: "waifu2x_swin_photo_noise1_2x.onnx",
            2: "waifu2x_swin_photo_noise2_2x.onnx",
            3: "waifu2x_swin_photo_noise3_2x.onnx",
        },
        "dn_only": None,
        "hint": 2,
        "sharp": (0.8, 0.6),
    },
    "esrgan": {
        "label": "通用场景",
        "desc": "照片和插画都行，适用范围最广",
        "tech": "Real-ESRGAN x4plus · 原生 4× · 68 MB · 单权重，无降噪档",
        "noise": {0: "RealESRGAN_x4plus.onnx"},
        "dn_only": None,
        "hint": 4,
        "sharp": (0.8, 0.6),
    },
    "anime": {
        "label": "动漫线稿",
        "desc": "二次元线稿专用，线条最硬",
        "tech": "4x-AnimeSharp · 原生 4× · 68 MB · 单权重，无降噪档",
        "noise": {0: "4x-AnimeSharp.onnx"},
        "dn_only": None,
        "hint": 4,
        "sharp": (1.0, 0.85),
    },
}

DENOISE_LEVELS = {"none": 0, "low": 1, "medium": 2, "high": 3, "highest": 3}

# 清晰度档位 -> 在预设基准锐度上再乘一个系数。
# 这三档是给用户「我要更接近线上那种硬线条」的入口，原样就是完全不做锐化。
CLEAR_LEVELS = {"soft": 0.0, "normal": 1.0, "crisp": 1.5}
CLEAR_LABELS = {"soft": "原样", "normal": "标准", "crisp": "更锐"}


def resolve_model(preset: str, denoise: str, scale: int = 2) -> str:
    if preset not in PRESETS:
        raise SystemExit(f"未知预设 '{preset}'，可选：{', '.join(PRESETS)}")
    p = PRESETS[preset]
    if scale == 1:
        if not p.get("dn_only"):
            raise SystemExit(f"预设 '{preset}' 没有“只降噪”权重，无法 1x 处理")
        path = os.path.join(MODEL_DIR, p["dn_only"])
    else:
        idx = DENOISE_LEVELS.get(denoise)
        if idx is None:
            raise SystemExit(f"未知降噪档 '{denoise}'，可选：{', '.join(DENOISE_LEVELS)}")
        if idx not in p["noise"]:
            idx = max(p["noise"])
        path = os.path.join(MODEL_DIR, p["noise"][idx])
    if not os.path.isfile(path):
        raise SystemExit(f"模型文件不存在：{path}\n请先运行 python download_models.py")
    return path


def plan_passes(scale: int, native: int) -> tuple[int, int]:
    """要串几次网络 + 网络实际输出倍率（不足或超出部分最后用 Lanczos 收尾）"""
    if scale == 1:
        if native != 1:
            raise SystemExit("所选模型不是 1x 模型，无法只降噪；请用 --scale 2 起")
        return 1, 1
    if native == 1:
        raise SystemExit("所选模型只能 1x 降噪，不能放大")
    k = max(1, int(math.ceil(math.log(scale) / math.log(native) - 1e-9)))
    full = native ** k
    if full == scale:
        return k, full
    if k > 1 and (native ** (k - 1)) * 2 >= scale:
        return k - 1, native ** (k - 1)
    return k, full


def resolve_sharpen(preset: str, clear: str | float) -> tuple[float, float]:
    """清晰度 -> 实际用的 (半径, 增益)。传数字就是直接指定增益。"""
    if isinstance(clear, (int, float)):
        gain = float(clear)
    else:
        mult = CLEAR_LEVELS.get(clear)
        if mult is None:
            raise SystemExit(f"未知清晰度 '{clear}'，可选：{', '.join(CLEAR_LEVELS)}")
        gain = PRESETS[preset].get("sharp", (0.0, 0.0))[1] * mult
    radius = PRESETS[preset].get("sharp", (0.0, 0.0))[0]
    return radius, gain


# --------------------------------------------------------------------------- #
# 收尾锐化（unsharp mask）
# --------------------------------------------------------------------------- #
def _gauss_kernel(radius: float) -> np.ndarray:
    k = max(1, int(math.ceil(radius * 3.0)))
    x = np.arange(-k, k + 1, dtype=np.float32)
    w = np.exp(-(x * x) / (2.0 * radius * radius))
    return (w / w.sum()).astype(np.float32)


def _gauss_blur(rgb: np.ndarray, radius: float, row_chunk: int = 256) -> np.ndarray:
    """可分离高斯模糊，float32，边界 reflect。

    按行分块做：16x 的大图有 3 亿多像素，整幅中转一下就是好几个 GB。
    分块后每个中转数组只跟 chunk 行有关，峰值内存跟图高无关。
    """
    w = _gauss_kernel(radius)
    k = len(w) // 2
    H, W, _ = rgb.shape
    out = np.empty_like(rgb)
    for y0 in range(0, H, row_chunk):
        y1 = min(H, y0 + row_chunk)
        y0e, y1e = max(0, y0 - k), min(H, y1 + k)
        blk = rgb[y0e:y1e]
        # 竖向
        pad = np.pad(blk, ((k, k), (0, 0), (0, 0)), mode="reflect")
        v = np.zeros_like(blk)
        for i, wi in enumerate(w):
            v += wi * pad[i:i + blk.shape[0]]
        # 横向
        pad2 = np.pad(v, ((0, 0), (k, k), (0, 0)), mode="reflect")
        o = np.zeros_like(v)
        for i, wi in enumerate(w):
            o += wi * pad2[:, i:i + blk.shape[1]]
        out[y0:y1] = o[y0 - y0e:y1 - y0e]
    return out


def unsharp(rgb: np.ndarray, radius: float, gain: float) -> np.ndarray:
    """边缘锐化：rgb + gain * (rgb - 模糊版)。gain<=0 直接原样返回。

    只加在「比模糊版更亮/更暗」的像素上，也就是边缘；平坦区 rgb≈模糊版，
    加了个零 —— 这是它不会把干净区域磨出噪点的原因。
    """
    if gain <= 0 or radius <= 0:
        return rgb
    return np.clip(rgb + gain * (rgb - _gauss_blur(rgb, float(radius))), 0.0, 1.0)


# --------------------------------------------------------------------------- #
# 图像读写
# --------------------------------------------------------------------------- #
def load_image(path: str) -> tuple[np.ndarray, np.ndarray | None, str]:
    im = Image.open(path)
    src_mode = im.mode
    im = im.convert("RGBA") if src_mode in ("RGBA", "LA", "P") else im.convert("RGB")
    if im.mode == "RGBA":
        arr = np.asarray(im, dtype=np.uint8)
        return (arr[:, :, :3].astype(np.float32) / 255.0,
                arr[:, :, 3].astype(np.float32) / 255.0, src_mode)
    arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
    return arr.astype(np.float32) / 255.0, None, src_mode


def save_image(rgb: np.ndarray, path: str, alpha: np.ndarray | None = None) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    arr = (np.clip(rgb, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    if alpha is not None:
        a = (np.clip(alpha, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
        im = Image.fromarray(np.dstack([arr, a]), mode="RGBA")
    else:
        im = Image.fromarray(arr, mode="RGB")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        im.convert("RGB").save(path, quality=95, subsampling=0, optimize=True)
    elif ext == ".webp":
        im.save(path, quality=95, method=6)
    else:
        im.save(path)


# --------------------------------------------------------------------------- #
# 推理核心
# --------------------------------------------------------------------------- #
class SRRunner:
    """单个 ONNX 模型的瓦片式推理器（自动适配尺寸约定）"""

    def __init__(self, model_path: str, device: str = "auto", tile: int = 256,
                 overlap: int = 16, threads: int | None = None, verbose: bool = True):
        self.model_path = model_path
        self.name = os.path.basename(model_path)
        self.tile = max(MIN_TILE, tile)
        self.overlap = max(0, min(overlap, self.tile // 2 - 1))
        self.verbose = verbose
        self.device = self._pick_device(device)
        self._cpu_retry = False
        self.sess = self._make_session(threads)
        self._read_io()
        self._fit_affine()
        if self.device == "dml":
            self._verify_device()

    # ---- 设备 / 会话 ----
    @staticmethod
    def _pick_device(device: str) -> str:
        avail = ort.get_available_providers()
        if device == "cpu":
            return "cpu"
        if "DmlExecutionProvider" in avail:
            return "dml"
        if device == "dml":
            raise SystemExit("当前 onnxruntime 不支持 DirectML，请装 onnxruntime-directml")
        return "cpu"

    def _make_session(self, threads: int | None):
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.enable_mem_pattern = False          # DirectML 要求
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.log_severity_level = 4
        if threads:
            so.intra_op_num_threads = threads
        providers = (["DmlExecutionProvider", "CPUExecutionProvider"]
                     if self.device == "dml" else ["CPUExecutionProvider"])
        try:
            return ort.InferenceSession(self.model_path, sess_options=so, providers=providers)
        except Exception:
            if self.device == "dml":
                if self.verbose:
                    print("  [warn] DirectML 初始化失败，改用 CPU")
                self.device = "cpu"
                return ort.InferenceSession(self.model_path, sess_options=so,
                                            providers=["CPUExecutionProvider"])
            raise

    def _read_io(self):
        i = self.sess.get_inputs()[0]
        o = self.sess.get_outputs()[0]
        self.in_name, self.out_name = i.name, o.name
        self.in_shape = list(i.shape)
        self.in_dtype = np.float16 if "float16" in i.type else np.float32
        self.fixed = len(self.in_shape) == 4 and all(
            isinstance(v, int) for v in self.in_shape[2:4])
        self.nhwc = _is_nhwc(self.in_shape)
        self.out_nhwc = False
        self.out_mul = 1.0
        self.scale = 1
        self.border = 0.0
        self.pad = 0
        self.crop = 0

    # ---- 前向 ----
    def _forward(self, x: np.ndarray) -> np.ndarray:
        """x 为 NCHW float32，返回模型原始输出"""
        xin = np.transpose(x, (0, 2, 3, 1)) if self.nhwc else x
        xin = xin.astype(self.in_dtype, copy=False)
        try:
            return self.sess.run([self.out_name], {self.in_name: xin})[0]
        except Exception:
            if self.device == "dml" and not self._cpu_retry:
                if self.verbose:
                    print("  [warn] DirectML 执行异常，整轮退回 CPU")
                self._cpu_retry = True
                self.__init__(self.model_path, "cpu", self.tile, self.overlap, verbose=False)
                return self._forward(x)
            raise

    def _as_nchw(self, raw) -> np.ndarray:
        y = np.asarray(raw, np.float32)
        if y.ndim != 4:
            raise SystemExit(f"{self.name} 输出维度异常：{y.shape}")
        if y.shape[-1] == 3 and y.shape[1] != 3:
            self.out_nhwc = True
            y = np.transpose(y, (0, 3, 1, 2))
        if float(np.nanmax(y)) > 1.5:
            self.out_mul = 1.0 / 255.0
        return y

    # ---- 自动拟合 out = a*in + b ----
    def _fit_affine(self):
        if self.fixed:
            s = int(self.in_shape[2])
            y = self._as_nchw(self._forward(np.zeros((1, 3, s, s), np.float32)))
            self.scale = max(1, y.shape[2] // s)
            self.border, self.pad, self.crop = 0.0, 0, 0
            return

        pts, last = [], None
        for s in (64, 96, 128, 192, 256, 320):
            try:
                y = self._as_nchw(self._forward(np.zeros((1, 3, s, s), np.float32)))
                pts.append((s, int(y.shape[2])))
                if len(pts) >= 2:
                    break
            except Exception as e:      # 太小的输入会被网络内部的 Pad 拒绝
                last = e
        if len(pts) < 2:
            raise SystemExit(f"无法推断 {self.name} 的尺寸规律：{last}")

        (s1, o1), (s2, o2) = pts
        a = (o2 - o1) / (s2 - s1)
        b = o1 - a * s1
        self.scale = max(1, int(round(a)))
        self.border = max(0.0, -b)

        if self.border > 0:
            self.pad = int(max(8, math.ceil(self.border / (2 * self.scale)) * 2))
            self.crop = max(0, int(round(self.scale * self.pad + b / 2.0)))
        else:
            self.pad = self.crop = 0

        # 验证：外扩 pad、跑一遍再裁 crop，应恰好得到 scale 倍
        t = MIN_TILE
        probe = np.pad(np.zeros((1, 3, t, t), np.float32), self._pad_width(t, t), mode="edge")
        y = self._as_nchw(self._forward(probe))
        got = y.shape[2] - 2 * self.crop
        if got != t * self.scale:
            raise SystemExit(f"{self.name} 尺寸规律自检失败：期望 {t*self.scale}，实得 {got}")

    def _pad_width(self, h: int, w: int):
        p = self.pad
        return ((0, 0), (0, 0), (p, p), (p, p)) if p else ((0, 0), (0, 0), (0, 0), (0, 0))

    # ---- GPU 后端数值自检 ----
    # DirectML 对某些网络会静默算错（尺寸正常、内容是废的），所以拿固定输入
    # 跟 CPU 结果对比一次；偏差过大就永久退回 CPU，并把结论缓存下来。
    @staticmethod
    def _raw_nchw(sess, x_nchw: np.ndarray, dtype) -> np.ndarray:
        inp = sess.get_inputs()[0]
        xin = np.transpose(x_nchw, (0, 2, 3, 1)) if _is_nhwc(inp.shape) else x_nchw
        y = np.asarray(sess.run(None, {inp.name: xin.astype(dtype)})[0], np.float32)
        if y.ndim == 4 and y.shape[-1] == 3 and y.shape[1] != 3:
            y = np.transpose(y, (0, 3, 1, 2))
        return y

    def _verify_device(self):
        key = f"{os.path.basename(self.model_path)}|{os.path.getsize(self.model_path)}"
        cache = {}
        if os.path.isfile(DEVICE_CACHE):
            try:
                with open(DEVICE_CACHE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                cache = {}
        verdict = cache.get(key)
        if verdict == "dml":
            return
        if verdict == "cpu":
            self._switch_to_cpu("缓存记录：该模型在 DirectML 上数值不正确")
            return

        x = np.random.default_rng(0).random((1, 3, MIN_TILE, MIN_TILE)).astype(np.float32)
        try:
            y_gpu = self._raw_nchw(self.sess, x, self.in_dtype)
            cpu = ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])
            y_cpu = self._raw_nchw(cpu, x, self.in_dtype)
        except Exception:
            return
        diff = float(np.max(np.abs(y_gpu - y_cpu))) if y_gpu.shape == y_cpu.shape else 1.0
        ok = diff <= 0.02
        cache[key] = "dml" if ok else "cpu"
        try:
            os.makedirs(MODEL_DIR, exist_ok=True)
            with open(DEVICE_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        if not ok:
            self._switch_to_cpu(f"DirectML 数值不正确（与 CPU 最大偏差 {diff:.3f}）")

    def _switch_to_cpu(self, reason: str):
        if self.verbose:
            print(f"  [warn] {reason}，本模型改用 CPU")
        self.device = "cpu"
        self._cpu_retry = True
        self.sess = self._make_session(None)

    # ---- 瓦片融合 ----
    @staticmethod
    def _ramp(n: int, o: int, first: bool, last: bool) -> np.ndarray:
        w = np.ones(n, np.float32)
        if o > 0:
            if not first:
                w[:o] = np.arange(1, o + 1, dtype=np.float32) / (o + 1.0)
            if not last:
                w[-o:] = np.arange(o, 0, -1, dtype=np.float32) / (o + 1.0)
        return w

    def _tiles_1d(self, n: int) -> list[int]:
        if n <= self.tile:
            return [0]
        step = self.tile - self.overlap
        pos, y = [], 0
        while y + self.tile < n:
            pos.append(y)
            y += step
        pos.append(n - self.tile)
        out: list[int] = []
        for p in pos:
            if not out or p > out[-1]:
                out.append(p)
        return out

    def _run_tile(self, x: np.ndarray, th: int, tw: int) -> np.ndarray:
        """x: 1,3,H,W 的整图；返回该瓦片 (1,3,scale*th,scale*tw)"""
        ph = -(-th // TILE_MULT) * TILE_MULT
        pw = -(-tw // TILE_MULT) * TILE_MULT
        patch = x[:, :, :th, :tw]
        if (ph, pw) != (th, tw):
            patch = np.pad(patch, ((0, 0), (0, 0), (0, ph - th), (0, pw - tw)), mode="edge")
        if self.pad:
            h, w = patch.shape[2], patch.shape[3]
            mode = "reflect" if self.pad < min(h, w) else "edge"
            patch = np.pad(patch, self._pad_width(h, w), mode=mode)
        y = self._as_nchw(self._forward(np.ascontiguousarray(patch))) * self.out_mul
        if self.crop:
            y = y[:, :, self.crop:self.crop + ph * self.scale,
                  self.crop:self.crop + pw * self.scale]
        return y[:, :, :th * self.scale, :tw * self.scale]

    def upscale(self, rgb: np.ndarray, progress=None) -> np.ndarray:
        """rgb: float32 HWC [0,1] -> float32 (H*scale, W*scale, 3)"""
        H, W, _ = rgb.shape
        s = self.scale
        x = np.transpose(rgb, (2, 0, 1))[None]
        acc = np.zeros((3, H * s, W * s), np.float32)
        wsum = np.zeros((H * s, W * s), np.float32)
        ys, xs = self._tiles_1d(H), self._tiles_1d(W)
        total, done = len(ys) * len(xs), 0
        t0 = time.time()
        for y in ys:
            for x0 in xs:
                th, tw = min(self.tile, H - y), min(self.tile, W - x0)
                patch = x[:, :, y:y + th, x0:x0 + tw]
                out = self._run_tile(patch, th, tw)[0]
                wy = self._ramp(th * s, self.overlap * s, y == 0, y + th >= H)
                wx = self._ramp(tw * s, self.overlap * s, x0 == 0, x0 + tw >= W)
                wmap = wy[:, None] * wx[None, :]
                acc[:, y * s:(y + th) * s, x0 * s:(x0 + tw) * s] += out * wmap
                wsum[y * s:(y + th) * s, x0 * s:(x0 + tw) * s] += wmap
                done += 1
                if progress:
                    progress(done, total, time.time() - t0)
        acc /= np.maximum(wsum, 1e-6)[None]
        return np.transpose(np.clip(acc, 0.0, 1.0), (1, 2, 0))


# --------------------------------------------------------------------------- #
# 端到端：多次串联 + 收尾缩放
# --------------------------------------------------------------------------- #
def build_runner(preset: str, scale: int, denoise: str = "medium", device: str = "auto",
                 tile: int = 256, overlap: int = 16) -> SRRunner:
    return SRRunner(resolve_model(preset, denoise, scale), device=device,
                    tile=tile, overlap=overlap)


def upscale(rgb: np.ndarray, preset: str, scale: int, denoise: str = "medium",
            device: str = "auto", tile: int = 256, overlap: int = 16,
            clear: str | float = "normal", on_stage=None,
            runner: SRRunner | None = None) -> np.ndarray:
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
    if gain > 0:
        if on_stage:
            on_stage(f"收尾锐化 · 半径 {radius}px 增益 {gain:.2f}", passes, passes)
        cur = unsharp(cur, radius, gain)
    return cur


def upscale_alpha(alpha: np.ndarray | None, scale: int) -> np.ndarray | None:
    if alpha is None:
        return None
    h, w = alpha.shape[0] * scale, alpha.shape[1] * scale
    im = Image.fromarray((np.clip(alpha, 0, 1) * 255 + 0.5).astype(np.uint8), mode="L")
    return np.asarray(im.resize((w, h), Image.LANCZOS), np.float32) / 255.0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _outname(name: str, scale: int) -> str:
    stem, ext = os.path.splitext(name)
    if ext.lower() in (".jpg", ".jpeg"):
        ext = ".png"          # 放大结果转 PNG，避免二次 JPEG 损失
    return f"{stem}_{scale}x{ext}"


def _print_info():
    print("设备：")
    print("  可用 provider：", ort.get_available_providers())
    print("  CPU 核心：", os.cpu_count())
    print("\n预设（原生倍率以运行时自动探测为准，这里给的是已知值）：")
    for k, v in PRESETS.items():
        files = list(v["noise"].values())
        have = sum(1 for f in files if os.path.isfile(os.path.join(MODEL_DIR, f)))
        dn = "有" if v.get("dn_only") and os.path.isfile(os.path.join(MODEL_DIR, v["dn_only"])) else "无"
        print(f"  {k:8s} ~{v['hint']}x  [{have}/{len(files)} 档权重就绪 | 1x降噪:{dn}]  "
              f"{v['desc']}  ({v.get('tech', '')})")
    print("\n本地模型：")
    if os.path.isdir(MODEL_DIR):
        tot = 0
        for f in sorted(os.listdir(MODEL_DIR)):
            if f.endswith(".onnx"):
                sz = os.path.getsize(os.path.join(MODEL_DIR, f))
                tot += sz
                print(f"  {sz/1048576:7.2f}MB  {f}")
        print(f"  合计 {tot/1048576:.1f}MB")
    else:
        print("  （还没下载，运行 python download_models.py）")


def main(argv=None):
    ap = argparse.ArgumentParser(description="BigPixels：深度卷积网络放大 2/4/8/16 倍")
    ap.add_argument("input", nargs="?", help="输入图片或目录")
    ap.add_argument("-o", "--output", help="输出图片或目录")
    ap.add_argument("--preset", default="art", help="预设：" + "/".join(PRESETS))
    ap.add_argument("--scale", type=int, default=4, choices=[1, 2, 4, 8, 16], help="放大倍率")
    ap.add_argument("--denoise", default="medium", help="降噪程度：" + "/".join(DENOISE_LEVELS))
    ap.add_argument("--clear", default="normal",
                    help="清晰度（收尾锐化）：" + "/".join(CLEAR_LEVELS) + "，也可直接给增益数字")
    ap.add_argument("--tile", type=int, default=256, help="瓦片边长（显存不够就调小）")
    ap.add_argument("--overlap", type=int, default=16, help="瓦片重叠像素")
    ap.add_argument("--device", default="auto", choices=["auto", "dml", "cpu"])
    ap.add_argument("--compare", action="store_true", help="同时输出双三次插值对照图")
    ap.add_argument("--list", action="store_true", help="列出预设")
    ap.add_argument("--info", action="store_true", help="显示设备与模型状态")
    args = ap.parse_args(argv)

    if args.list:
        for k, v in PRESETS.items():
            print(f"{k:8s} ~{v['hint']}x  {v['desc']}  ({v.get('tech', '')})")
            for idx, f in v["noise"].items():
                print(f"          降噪档 {idx} -> {f}")
            r, g = v.get("sharp", (0, 0))
            print("          清晰度 标准 -> 半径 %.1fpx / 增益 %.2f" % (r, g))
        print("\n清晰度档位：" + " / ".join(
            f"{k}(×{v:g})" for k, v in CLEAR_LEVELS.items()))
        return
    if args.info:
        _print_info()
        return
    if not args.input:
        ap.print_help()
        return

    src = args.input
    if os.path.isdir(src):
        files = [os.path.join(src, f) for f in sorted(os.listdir(src))
                 if os.path.splitext(f)[1].lower() in IMG_EXT]
        outdir = args.output or os.path.join(ROOT, "outputs")
        targets = [(f, os.path.join(outdir, _outname(os.path.basename(f), args.scale)))
                   for f in files]
    else:
        out = args.output or os.path.join(ROOT, "outputs",
                                         _outname(os.path.basename(src), args.scale))
        targets = [(src, out)]
    if not targets:
        raise SystemExit("没找到可处理的图片")

    path = resolve_model(args.preset, args.denoise, args.scale)
    runner = SRRunner(path, device=args.device, tile=args.tile, overlap=args.overlap)
    passes, net_scale = plan_passes(args.scale, runner.scale)
    print(f"模型 {runner.name}")
    print(f"  设备 {runner.device.upper()} | 网络原生 {runner.scale}x | 目标 {args.scale}x "
          f"| 串联 {passes} 次 | 外扩 {runner.pad}px / 裁回 {runner.crop}px | 瓦片 {runner.tile}(+{runner.overlap})")
    _r, _g = resolve_sharpen(args.preset, args.clear)
    print(f"  清晰度 {args.clear}（锐化半径 {_r}px · 增益 {_g:.2f}）")

    for i, (f, out) in enumerate(targets, 1):
        rgb, alpha, mode = load_image(f)
        h0, w0 = rgb.shape[:2]
        print(f"[{i}/{len(targets)}] {os.path.basename(f)}  {w0}x{h0}（{mode}）")
        t0 = time.time()
        cur = rgb
        for p in range(passes):
            state = {"last": -1}

            def prog(done, total, el, _bar=state):
                pct = int(done * 100 / total)
                if pct >= _bar["last"] + 10 or done == total:
                    _bar["last"] = pct
                    eta = (el / done) * (total - done) if done else 0
                    print(f"    瓦片 {done}/{total}  {pct:3d}%  已用 {el:5.1f}s  还剩约 {eta:5.1f}s",
                          flush=True)

            if passes > 1:
                print(f"    网络推理 {p + 1}/{passes}")
            cur = runner.upscale(cur, progress=prog)
        if net_scale != args.scale:
            th, tw = h0 * args.scale, w0 * args.scale
            cur = np.asarray(Image.fromarray((cur * 255 + 0.5).astype(np.uint8))
                             .resize((tw, th), Image.LANCZOS), np.float32) / 255.0
        radius, gain = resolve_sharpen(args.preset, args.clear)
        if gain > 0:
            print(f"    收尾锐化 半径 {radius}px · 增益 {gain:.2f}")
            cur = unsharp(cur, radius, gain)
        save_image(cur, out, upscale_alpha(alpha, args.scale))
        print(f"    -> {out}  {cur.shape[1]}x{cur.shape[0]}  用时 {time.time()-t0:.1f}s")

        if args.compare:
            base = Image.fromarray((rgb * 255 + 0.5).astype(np.uint8)).resize(
                (cur.shape[1], cur.shape[0]), Image.LANCZOS).convert("RGB")
            cmp_path = os.path.splitext(out)[0] + "_bicubic.jpg"
            base.save(cmp_path, quality=95, subsampling=0)
            print(f"    -> {cmp_path}（双三次插值对照）")


if __name__ == "__main__":
    main()
