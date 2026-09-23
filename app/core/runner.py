#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""瓦片式推理器：单个 ONNX 模型的尺寸约定自适配 + 重叠羽化融合 + 后端数值自检。"""
from __future__ import annotations

import json
import math
import os
import time

import numpy as np
from PIL import Image

from .paths import DEVICE_CACHE, MODEL_DIR

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover
    import sys
    sys.exit("缺少 onnxruntime，请先执行:  .\\.venv\\Scripts\\python.exe -m pip install onnxruntime-directml")


# 瓦片送入网络前先补齐到这个倍数，避免网络内部下采样出现非整除尺寸
TILE_MULT = 32
MIN_TILE = 64


def _is_nhwc(shape) -> bool:
    """判断 4 维张量布局是否为 NHWC（通道在最后）"""
    return len(shape) == 4 and shape[-1] == 3 and shape[1] != 3


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

    def upscale(self, rgb: np.ndarray, progress=None,
                post_down: int = 1) -> np.ndarray:
        """rgb: float32 HWC [0,1] -> float32 (H*scale//post_down, W*scale//post_down, 3)

        post_down > 1 时「每一块出网络后先降采样再叠加」，所以中间那层超大图
        从不整幅驻留内存 —— 「优化算法版」的第二次网络（4x 图再跑一次得到 16x）
        就走这条路。降采样在 float 上做（PIL 的 F 模式），不先量化到 8 位。
        """
        H, W, _ = rgb.shape
        s = self.scale
        pd = max(1, int(post_down))
        if s % pd:
            raise SystemExit(f"post_down({pd}) 必须整除网络倍率({s})")
        os_ = s // pd                      # 叠加与输出用的倍率
        x = np.transpose(rgb, (2, 0, 1))[None]
        acc = np.zeros((3, H * os_, W * os_), np.float32)
        wsum = np.zeros((H * os_, W * os_), np.float32)
        oo = self.overlap * os_ // pd      # 羽毛边在输出尺度上的像素数
        ys, xs = self._tiles_1d(H), self._tiles_1d(W)
        total, done = len(ys) * len(xs), 0
        t0 = time.time()
        for y in ys:
            for x0 in xs:
                th, tw = min(self.tile, H - y), min(self.tile, W - x0)
                patch = x[:, :, y:y + th, x0:x0 + tw]
                out = self._run_tile(patch, th, tw)[0]          # (3, th*s, tw*s)
                if pd > 1:
                    oh, ow = th * os_, tw * os_
                    plane = np.transpose(np.clip(out, 0.0, 1.0), (1, 2, 0))
                    out = np.empty((3, oh, ow), np.float32)
                    for c in range(3):
                        out[c] = np.asarray(
                            Image.fromarray(plane[:, :, c], mode="F")
                            .resize((ow, oh), Image.LANCZOS), np.float32)
                oy, ox = y * os_, x0 * os_
                wy = self._ramp(th * os_, oo, y == 0, y + th >= H)
                wx = self._ramp(tw * os_, oo, x0 == 0, x0 + tw >= W)
                wmap = wy[:, None] * wx[None, :]
                acc[:, oy:oy + th * os_, ox:ox + tw * os_] += out * wmap
                wsum[oy:oy + th * os_, ox:ox + tw * os_] += wmap
                done += 1
                if progress:
                    progress(done, total, time.time() - t0)
        acc /= np.maximum(wsum, 1e-6)[None]
        return np.transpose(np.clip(acc, 0.0, 1.0), (1, 2, 0))


