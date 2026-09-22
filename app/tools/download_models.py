#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BigPixels 模型自检 + 下载

权重一共 15 个、约 280 MB，不能塞进 git 仓库（GitHub 单文件 100 MB 就拦，
何况每次改代码都要重传），所以仓库里只有这份清单，权重按需现下。

三件事：
  1. 自检 —— 每个模型「在不在、全不全」。只有大小跟清单**一个字节不差**才算数；
     半截文件（断网、被中断、LFS 指针、下载站返回的 HTML 错误页）都算缺，
     下次运行会重下它，而不是让 onnxruntime 在几分钟后抛一个看不懂的错。
  2. 下载 —— 只下缺的那些。断点续传（`.part` 记着进度），失败重试 3 次，
     全部写完才改名为正式文件名，所以「文件存在」永远等于「文件完整」。
  3. 退出码 —— 给双击启动的脚本用：
       0 全齐（或补全成功）
       1 补不齐（网络断了之类，提示里会说清怎么重试）
       2 参数或环境有问题

用法：
    python tools/download_models.py               # 自检 + 缺什么下什么（双击启动脚本走这条）
    python tools/download_models.py --check       # 只自检，不下载。缺了返回 1
    python tools/download_models.py --force       # 忽略已有文件，全部重下
    python tools/download_models.py --source hf   # 走 huggingface.co（默认走国内的 hf-mirror）
    python tools/download_models.py --dir D:\\x    # 换个模型目录（默认项目根下的 models/）

    python tools/upscale.py --info                # 查看引擎当前的模型状态
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.request

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.core.paths import MODEL_DIR                                   # noqa: E402
from app.core.i18n import t, set_lang                                 # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DIR = MODEL_DIR

CHUNK = 4 * 1024 * 1024          # 一次要 4 MB，够大不至于把请求数堆起来
TRIES = 3
HDR = {"User-Agent": "BigPixels/1.0 (+model fetch)"}

SOURCES = {
    "hf": "https://huggingface.co",       # 原站，墙外快
    "mirror": "https://hf-mirror.com",    # 国内镜像，默认
}

W_REPO = "deepghs/waifu2x_onnx"
W_PATH = "20250502/onnx_models"
E_REPO = "yuvraj108c/ComfyUI-Upscaler-Onnx"

# --------------------------------------------------------------------------- #
# 清单：仓库 / 仓库内路径 / 本地文件名 / 字节数
#
# 字节数是照着上游实际文件记的，不是估的 —— 自检就靠它判「全不全」。
# 上游哪天换权重，这里的大小会先对不上，提示里会告诉你哪个文件不符，
# 而不是含糊地说一句「下载失败」。
# --------------------------------------------------------------------------- #
MODELS = [
    # waifu2x · CUnet · 卡通（每档 4.9 MB，四个降噪档）
    (W_REPO, f"{W_PATH}/cunet/art/noise0_scale2x.onnx",
     "waifu2x_cunet_art_noise0_2x.onnx", 5178591),
    (W_REPO, f"{W_PATH}/cunet/art/noise1_scale2x.onnx",
     "waifu2x_cunet_art_noise1_2x.onnx", 5178591),
    (W_REPO, f"{W_PATH}/cunet/art/noise2_scale2x.onnx",
     "waifu2x_cunet_art_noise2_2x.onnx", 5178591),
    (W_REPO, f"{W_PATH}/cunet/art/noise3_scale2x.onnx",
     "waifu2x_cunet_art_noise3_2x.onnx", 5178591),
    # 只降噪（1x）那一份。上游给的就是个 1542 字节的占位文件，模型本身不能用 ——
    # 见 README 的「已知问题」。清单里仍然列着、大小照实记，这样自检不会对它撒谎。
    (W_REPO, f"{W_PATH}/cunet/art/scale1x.onnx",
     "waifu2x_cunet_art_dn1x.onnx", 1542),

    # waifu2x · Swin-UNet · 卡通（每档 15.9 MB）
    (W_REPO, f"{W_PATH}/swin_unet/art/noise0_scale2x.onnx",
     "waifu2x_swin_art_noise0_2x.onnx", 16727049),
    (W_REPO, f"{W_PATH}/swin_unet/art/noise1_scale2x.onnx",
     "waifu2x_swin_art_noise1_2x.onnx", 16727049),
    (W_REPO, f"{W_PATH}/swin_unet/art/noise2_scale2x.onnx",
     "waifu2x_swin_art_noise2_2x.onnx", 16727049),
    (W_REPO, f"{W_PATH}/swin_unet/art/noise3_scale2x.onnx",
     "waifu2x_swin_art_noise3_2x.onnx", 16727049),

    # waifu2x · Swin-UNet · 照片（每档 18.0 MB）
    (W_REPO, f"{W_PATH}/swin_unet/photo/noise0_scale2x.onnx",
     "waifu2x_swin_photo_noise0_2x.onnx", 18905935),
    (W_REPO, f"{W_PATH}/swin_unet/photo/noise1_scale2x.onnx",
     "waifu2x_swin_photo_noise1_2x.onnx", 18905935),
    (W_REPO, f"{W_PATH}/swin_unet/photo/noise2_scale2x.onnx",
     "waifu2x_swin_photo_noise2_2x.onnx", 18905935),
    (W_REPO, f"{W_PATH}/swin_unet/photo/noise3_scale2x.onnx",
     "waifu2x_swin_photo_noise3_2x.onnx", 18905935),

    # Real-ESRGAN 系（各 68.3 MB，一份就把体积占了快一半）
    (E_REPO, "RealESRGAN_x4.onnx", "RealESRGAN_x4plus.onnx", 71636302),
    (E_REPO, "4x-AnimeSharp.onnx", "4x-AnimeSharp.onnx", 71636302),
]

GROUP = {
    "waifu2x_cunet_": "dl.group.cunet_art",
    "waifu2x_swin_art_": "dl.group.swin_art",
    "waifu2x_swin_photo_": "dl.group.swin_photo",
    "RealESRGAN_": "dl.group.esrgan",
    "4x-AnimeSharp": "dl.group.animesharp",
}

TOTAL_BYTES = sum(m[3] for m in MODELS)


def mb(n: float) -> str:
    return f"{n / 1048576:.1f} MB"


def size_str(n: int) -> str:
    """小文件按 KB 说 —— 那个 1542 字节的占位权重显示成 0.0 MB 就等于没说，
    但也要保住那一列的对齐（'1.5 KB' 和 '4.9 MB' 一样宽）。"""
    return mb(n) if n >= 1048576 else f"{n / 1024:.1f} KB"


def group_of(name: str) -> str:
    for k, v in GROUP.items():
        if name.startswith(k):
            return v
    return "dl.group.other"


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #
def inspect(dest: str) -> tuple[list[tuple], list[tuple]]:
    """返回 (齐的, 缺的/坏的)。每项是 (仓库, 路径, 文件名, 字节数, 现状说明)"""
    good, bad = [], []
    for repo, path, name, size in MODELS:
        p = os.path.join(dest, name)
        if not os.path.exists(p):
            bad.append((repo, path, name, size, "缺"))
            continue
        got = os.path.getsize(p)
        if got == size:
            good.append((repo, path, name, size, "在"))
            continue
        # 大小不对：半截文件、LFS 指针、下载站塞回来的 HTML 错误页，三种都要说清是哪种。
        # ONNX 是 protobuf，第一个字节必是 0x08（ir_version 那个字段）—— 拿它当门槛最准。
        head = open(p, "rb").read(64)
        low = head.lower()
        if head.startswith(b"version https://git-lfs"):
            why = t("dl.why.lfs")
        elif low.startswith(b"<!doctype") or low.startswith(b"<?xml") or b"<html" in low:
            why = t("dl.why.html")
        elif head[:1] != b"\x08":
            why = t("dl.why.notonnx")
        else:
            why = t("dl.status.partial")
        bad.append((repo, path, name, size,
                    t("dl.mismatch", why=why, got=got, size=size)))
    return good, bad


def report(good: list, bad: list, dest: str, check_only: bool):
    print("\n" + t("dl.title", dir=dest))
    print(t("dl.manifest", n=len(MODELS), size=size_str(TOTAL_BYTES)) + "\n")
    if good:
        last = None
        for _repo, _path, name, size, _why in good:
            g = group_of(name)
            if g != last:
                print("[" + t(g) + "]")
                last = g
            print(t("dl.row.have", name=name, size=size_str(size)))
    if bad:
        print()
        last = None
        for _repo, _path, name, size, why in bad:
            g = group_of(name)
            if g != last:
                print("[" + t(g) + "]")
                last = g
            print(t("dl.row.missing", name=name, size=size_str(size), why=why))
    print()
    line = t("dl.sum", have=len(good), miss=len(bad))
    if bad and not check_only:
        line += t("dl.sum.todo", size=size_str(sum(b[3] for b in bad)))
    print(line)
    if not bad:
        print(t("dl.all_ok") + "\n")


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def head_size(url: str) -> int:
    """问服务端这个文件多大。Range 请求比 GET 便宜，有些站不认就退回 Content-Length。"""
    req = urllib.request.Request(url, headers={**HDR, "Range": "bytes=0-1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        cr = r.headers.get("Content-Range")
        if cr:
            m = re.search(r"/(\d+)\s*$", cr)
            if m:
                return int(m.group(1))
        return int(r.headers.get("Content-Length") or 0)


def fetch(base: str, repo: str, path: str, name: str, want: int, dest: str) -> bool:
    """下这一个文件。返回是否成功。

    断点续传：`.part` 里已经有多少字节就从哪儿接着要（服务端支持 Range 的话）。
    只有严格等于期望大小才 os.replace 成正式名字 —— 所以「文件在」就等于「文件全」，
    自检才敢只看大小。
    """
    out = os.path.join(dest, name)
    tmp = out + ".part"
    url = f"{base}/{repo}/resolve/main/{path}"

    for attempt in range(1, TRIES + 1):
        try:
            total = head_size(url) or want
            if total != want:
                print("\n" + t("dl.upstream", total=total, want=want))
                return False
            have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if have > total:                    # 上回留下的脏 .part
                have = 0
            got, t0 = have, time.time()
            mode = "ab" if have else "wb"
            if have:
                print("\n" + t("dl.resume", name=name, have=mb(have)))
            with open(tmp, mode) as f:
                while got < total:
                    end = min(got + CHUNK, total) - 1
                    req = urllib.request.Request(
                        url, headers={**HDR, "Range": f"bytes={got}-{end}"})
                    with urllib.request.urlopen(req, timeout=180) as r:
                        once = r.status == 200      # 服务端无视 Range，一次性给了全量
                        data = r.read()
                    if not data:
                        break
                    if once and got:                # 从头再来，把之前的丢掉
                        f.seek(0)
                        f.truncate()
                        got = 0
                    f.write(data)
                    got += len(data)
                    dt = max(time.time() - t0, 0.001)
                    sys.stdout.write(
                        f"\r      {name:<38s} {got * 100 // total:3d}%  "
                        f"{mb(got):>9s} / {mb(total):<9s} {mb((got - have) / dt)}/s   ")
                    sys.stdout.flush()
                    if once:
                        break
            if got != total:
                raise IOError(f"只拿到 {got}/{total} 字节")
            os.replace(tmp, out)
            # 结尾补空格把上面那行进度条盖干净 —— 进度行比这句长，
            # 不盖的话命令行里会留半截百分比（cmd 不一定吃 ANSI 的擦除序列）
            done = t("dl.file.done", name=name, size=mb(total),
                     part=mb(got - have), sec=time.time() - t0)
            print("\r" + done.ljust(90))
            return True
        except Exception as e:                  # 断网、超时、对端 5xx…
            msg = f"{type(e).__name__}: {str(e)[:80]}"
            print("\r" + t("dl.attempt", attempt=attempt, tries=TRIES,
                            name=name, msg=msg))
            if attempt < TRIES:
                time.sleep(2 * attempt)         # 退避一下再试，别把对端惹毛
    print(t("dl.fetch_fail", name=name))
    return False


def download(bad: list, dest: str, base: str) -> list:
    print(t("dl.start", base=base) + "\n")
    failed, t0 = [], time.time()
    for repo, path, name, size, _why in bad:
        if not fetch(base, repo, path, name, size, dest):
            failed.append(name)
    if len(bad) > len(failed):
        got = sum(b[3] for b in bad if b[2] not in failed)
        print("\n" + t("dl.batch", n=len(bad) - len(failed),
                        size=size_str(got), sec=time.time() - t0))
    return failed


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description=t("dl.desc"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=t("dl.exitcode"))
    ap.add_argument("--dir", default=os.environ.get("BIGPIXELS_MODELS") or DEFAULT_DIR,
                    help=t("dl.arg.dir"))
    ap.add_argument("--check", action="store_true", help=t("dl.arg.check"))
    ap.add_argument("--force", action="store_true", help=t("dl.arg.force"))
    ap.add_argument("--source", choices=sorted(SOURCES), default="mirror",
                    help=t("dl.arg.source"))
    ap.add_argument("--lang", choices=("zh", "en", "ja"), default=None,
                    help=t("lang.arghelp"))
    a = ap.parse_args()

    if a.lang:
        set_lang(a.lang)

    dest = os.path.abspath(a.dir)
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        print(t("dl.mkdir.fail", dest=dest, err=e))
        return 2

    if a.force:
        for _repo, _path, name, _size in MODELS:
            for p in (os.path.join(dest, name), os.path.join(dest, name + ".part")):
                if os.path.exists(p):
                    os.remove(p)

    good, bad = inspect(dest)
    report(good, bad, dest, a.check)

    if not bad:
        return 0
    if a.check:
        print(t("dl.check_only") + "\n")
        return 1

    print(t("dl.total", size=size_str(sum(b[3] for b in bad))))
    failed = download(bad, dest, SOURCES[a.source])

    # 下完再自检一遍 —— 不拿「请求返回 200」当成功，拿「文件真对得上」当成功
    good2, bad2 = inspect(dest)
    print()
    if not bad2:
        print(t("dl.done", n=len(good2), size=size_str(TOTAL_BYTES)) + "\n")
        return 0
    print(t("dl.still_missing", n=len(bad2), names=", ".join(b[2] for b in bad2)))
    print(t("dl.failed"))
    print(t("dl.failed.l1"))
    print(t("dl.failed.l2"))
    print(t("dl.failed.l3") + "\n")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n" + t("dl.interrupt") + "\n")
        sys.exit(1)
