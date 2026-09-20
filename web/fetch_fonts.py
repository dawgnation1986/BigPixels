#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把界面用到的三款字体（Archivo / IBM Plex Sans / IBM Plex Mono）抓成本地 woff2，
放进 web/fonts/，这样页面离线可用、不依赖任何 CDN。

    python web/fetch_fonts.py            # 缺什么抓什么
    python web/fetch_fonts.py --force    # 全部重抓（换字体时用）

只抓 latin 子集：界面正文是中文，交给系统字体渲染；拉丁字面只需要覆盖
数字、单位（×  · dB）、模型名和标题。

另外单独抓一份「符号子集」：IBM Plex 的 latin 子集里带 → (U+2192) 和 ≈ (U+2248)，
少了这两个，计量表里的箭头会掉进系统字体、跟等宽数字对不齐。

坑：Google 的子集注释写在 @font-face **前面**，必须连着注释一起匹配，
否则每个 face 都会退化成默认名，同一个字重被写好几次、互相覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# (字体名, 需要的字重)
WANT = [
    ("Archivo",       [600, 700]),
    ("IBM Plex Sans", [400, 500, 600]),
    ("IBM Plex Mono", [400, 500, 600]),
]

# 额外符号子集：(字体名, 需要的字符, 落盘文件名, unicode-range)
SYMS = [
    ("IBM Plex Mono", "→≈", "plex-mono-symbols.woff2", "U+2192,U+2248"),
]

CSS = "https://fonts.googleapis.com/css2?family={fam}&display=swap"
CSS_W = "https://fonts.googleapis.com/css2?family={fam}:wght@{w}&display=swap"


def slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://fonts.googleapis.com/"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read()


def latin_faces(css: str) -> list[dict]:
    """按 /* latin */ 分块，取出字重和 woff2 地址。"""
    out: dict[int, dict] = {}
    parts = re.split(r"/\*\s*([\w-]+)\s*\*/", css)
    for i in range(1, len(parts) - 1, 2):
        if parts[i] != "latin":
            continue
        block = parts[i + 1]
        w = re.search(r"font-weight:\s*(\d+)", block)
        u = re.search(r"url\((https://[^)]+\.woff2)\)", block)
        if w and u:
            out.setdefault(int(w.group(1)), {"weight": int(w.group(1)),
                                             "url": u.group(1)})
    return [out[k] for k in sorted(out)]


def any_face(css: str) -> str | None:
    """符号子集的地址是 /l/font?kit=... 这种带查询串的形式，不以 .woff2 结尾。"""
    u = re.search(r"url\((https://[^)]+)\)\s*format\('woff2'\)", css)
    return u.group(1) if u else None


def face_css(family: str, weight: str, file: str, urange: str) -> str:
    return ("@font-face{font-family:'%s';font-style:normal;font-weight:%s;"
            "font-display:swap;src:url('/fonts/%s') format('woff2');"
            "unicode-range:%s;}" % (family, weight, file, urange))


LATIN_RANGE = ("U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,"
               "U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+2074,U+20AC,"
               "U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD")


def fetch_one(url: str, filename: str, force: bool) -> str | None:
    dst = os.path.join(FONT_DIR, filename)
    if os.path.isfile(dst) and not force and os.path.getsize(dst) > 800:
        print(f"  = {filename} 已存在，跳过")
        return filename
    try:
        data = get(url)
    except Exception as e:
        print(f"  ! {filename} 下载失败：{e}")
        return None
    with open(dst, "wb") as f:
        f.write(data)
    print(f"  + {filename}  {len(data) / 1024:.1f} KB")
    return filename


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    os.makedirs(FONT_DIR, exist_ok=True)

    faces, meta = [], []

    for family, weights in WANT:
        print(f"[{family}]")
        for w in weights:
            # 一个字重一个请求。合并成 wght@600;700 时，Google 会返回「字重区间」
            # 形式的 face，同一份文件被两个字重引用 —— 落盘就成了两份一样的字节。
            try:
                css = get(CSS_W.format(fam=urllib.parse.quote(family), w=w)).decode()
            except Exception as e:
                print(f"  ! {w} 取 CSS 失败：{e}")
                continue
            found = {f["weight"]: f["url"] for f in latin_faces(css)}
            if w not in found:
                print(f"  ! CSS 里没有 {w} 的 latin face")
                continue
            fn = f"{slug(family)}-{w}.woff2"
            if fetch_one(found[w], fn, args.force):
                faces.append(face_css(family, str(w), fn, LATIN_RANGE))
                meta.append({"family": family, "weight": w, "file": fn})

    for family, chars, fn, urange in SYMS:
        print(f"[{family} 符号子集 {chars}]")
        url = (CSS.format(fam=urllib.parse.quote(family))
               + "&text=" + urllib.parse.quote(chars))
        try:
            css = get(url).decode()
        except Exception as e:
            print(f"  ! 取 CSS 失败：{e}")
            continue
        u = any_face(css)
        if u and fetch_one(u, fn, args.force):
            # 这个子集只有 400 一份，声明成 400-600 免得高字重被浏览器伪加粗
            faces.append(face_css(family, "400 600", fn, urange))
            meta.append({"family": family, "weight": "400-600", "file": fn,
                         "range": urange})

    with open(os.path.join(FONT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 72)
    print("贴进 index.html <style> 最前面：\n")
    for fa in faces:
        print(fa)
    print("=" * 72)


if __name__ == "__main__":
    main()
