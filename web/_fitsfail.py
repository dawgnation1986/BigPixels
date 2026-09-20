#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性：证明 shots.py 里「同屏」那条断言不是空转的。

做法：先按现在的样式量一遍（该过），再把 .lightbox 的 --stageCap 强制改回改版前的
56dvh（就是在用户窗口里量出「差 196 px」的那个值），再量一遍 —— 断言必须当场翻脸。
探针自己骗自己的坑上一轮踩过一次（框框那条），所以这条也照同样的规矩验一遍。

    python web/_fitsfail.py --job 9f0ff8556cdd --size 1920,1080
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PROFILE = os.path.join(ROOT, ".cache", "cdp-shots")

import shots                    # noqa: E402
from cdp import Session         # noqa: E402


def sample(s: Session) -> dict:
    return s.js(shots.CHECK)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--job", required=True)
    ap.add_argument("--size", default="1920,1080")
    a = ap.parse_args()
    base = f"http://{a.host}:{a.port}/"
    W, H = (int(v) for v in a.size.split(","))
    os.makedirs(PROFILE, exist_ok=True)

    with Session(base, size=(W, H), profile=PROFILE) as s:
        if not s.ready():
            raise SystemExit("页面没起来")
        s.call("Page.addScriptToEvaluateOnNewDocument", source=shots.TRAP)
        s.call("Page.navigate", url=base + "?theme=light&job=" + a.job)
        time.sleep(0.6)
        s.ready()
        shots.hover_stage(s, 0.5, 0.5)
        shots.wait_loupe(s)
        time.sleep(0.5)

        print("\n  ===== 一、按现在的样式（该过） =====")
        now = sample(s)
        ok1 = shots.report_check(now)

        # 把光台的长边钉回改版前的 56dvh。行内样式比 .lightbox:has(...) 那条更硬，压得住。
        s.js("document.querySelector('.lightbox')"
             ".style.setProperty('--stageCap','min(56dvh,620px)')")
        time.sleep(0.5)

        print("\n  ===== 二、把光台长边改回 56dvh（该当场翻脸） =====")
        old = sample(s)
        ok2 = shots.report_check(old)

    f1, f2 = now.get("fit") or {}, old.get("fit") or {}
    print(f"\n  现在：光台 {f1.get('stageH')} + 间隔 {f1.get('gap')} + 放大镜 "
          f"{f1.get('loupeH')} = {f1.get('need')} / 视口 {f1.get('vh')} → "
          f"fits={f1.get('fits')}，检查{'通过' if ok1 else '不通过'}")
    print(f"  改回：光台 {f2.get('stageH')} + 间隔 {f2.get('gap')} + 放大镜 "
          f"{f2.get('loupeH')} = {f2.get('need')} / 视口 {f2.get('vh')} → "
          f"fits={f2.get('fits')}，检查{'通过' if ok2 else '不通过'}")
    verdict = ok1 and not ok2
    print("\n  " + ("断言是活的：改版前必挂、改版后必过 ✓" if verdict
                    else "！！断言没起作用，得回去看 —— 两遍结果一样"))


if __name__ == "__main__":
    main()
