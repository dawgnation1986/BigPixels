#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性：把右栏从「对照台」那一行到放大镜底部的每一块高度量出来。

用户的原话是「鱼和熊掌不可兼得了」—— 光台上的框框和放大镜不在同一屏里。
要改版式就得先知道这 1178 px 到底分给了谁，不能猜。
用量出来的数字定新的尺寸，改完再用量出来的数字对账。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PROFILE = os.path.join(ROOT, ".cache", "cdp-layout")

from app.qa.cdp import Session          # noqa: E402

DUMP = r"""
(() => {
  const q = s => document.querySelector(s);
  const R = s => {
    const e = q(s);
    if (!e) return null;
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    return {
      top: Math.round(r.top + scrollY), h: Math.round(r.height), w: Math.round(r.width),
      mt: cs.marginTop, mb: cs.marginBottom, pt: cs.paddingTop, pb: cs.paddingBottom,
      disp: cs.display, ar: cs.aspectRatio
    };
  };
  const lb = q('.lightbox');
  const win = q('#paneIn');
  return {
    vh: innerHeight, vw: innerWidth,
    lightboxGap: lb ? getComputedStyle(lb).rowGap : null,
    benchCols: q('.bench') ? getComputedStyle(q('.bench')).gridTemplateColumns : null,
    boxes: {
      topbar: R('.topbar'), bar: R('.lightbox > .bar'), stagebox: R('.stagebox'),
      viewport: R('#viewport'), toolbar: R('.toolbar'), saveNote: R('#saveNote'),
      loupeSlot: R('#loupeSlot'), loupe: R('#loupe'), loupeBar: R('#loupe > .bar'),
      spanRow: R('#spanRow'), panes: R('#panes'), paneWin: R('#paneIn'),
      cap: R('#panes .pane:nth-child(1) .cap'), zoomNote: R('#zoomNote'),
      bicNote: R('#bicNote'), ledger: R('#ledgerWrap')
    },
    paneBox: win ? (() => {
      const r = win.getBoundingClientRect();
      return {w: Math.round(r.width), h: Math.round(r.height)};
    })() : null,
    paneXs: (() => {
      const row = q('#panes');
      if (!row) return null;
      const rr = row.getBoundingClientRect();
      return [...row.querySelectorAll('.pane .win')].map(e =>
        Math.round(e.getBoundingClientRect().left - rr.left));
    })(),
    stageBoxStyle: q('.stagebox') ? (() => {
      const cs = getComputedStyle(q('.stagebox'));
      return {pad: cs.padding, minH: cs.minHeight};
    })() : null,
    vpMaxW: q('#viewport') ? getComputedStyle(q('#viewport')).maxWidth : null
  };
})()
"""


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
        s.call("Page.navigate", url=base + "?theme=light&job=" + a.job)
        time.sleep(0.5)
        s.ready()
        b = s.box("#viewport")
        if b:
            s.mouse(int(b["x"] + b["w"] * 0.5), int(b["y"] + b["h"] * 0.5))
        s.wait_for("(() => {const l = document.querySelector('#loupe');"
                   "return l && !l.hidden && document.querySelector('#panes')"
                   "&& document.querySelector('#panes').style.getPropertyValue('--tilew');})()",
                   timeout=25.0)
        time.sleep(0.6)
        s.js("scrollTo(0, 0)")
        time.sleep(0.3)
        d = s.js(DUMP)

    print(f"\n  视口 {d['vw']}×{d['vh']}  ·  .lightbox row-gap {d['lightboxGap']}")
    print(f"  bench 列 {d['benchCols']}  ·  #viewport max-width {d['vpMaxW']}")
    print(f"  光台内边距 {d['stageBoxStyle']}")
    print(f"  三格一格 {d['paneBox']}  ·  三格左沿 {d['paneXs']}\n")
    print("    块                  top     高     宽     margin(t/b)      padding(t/b)      display")
    order = ["topbar", "bar", "stagebox", "viewport", "toolbar", "saveNote",
             "loupeSlot", "loupe", "loupeBar", "spanRow", "panes", "paneWin",
             "cap", "zoomNote", "bicNote", "ledger"]
    prev = None
    for k in order:
        r = d["boxes"].get(k)
        if not r:
            print(f"    {k:<16}  —")
            continue
        gap = ""
        if prev is not None and r["top"] >= prev:
            gap = f"   ← 距上一块 {r['top'] - prev} px"
        print("    %-16s %7d %6d %6d  %-14s %-14s %-10s%s"
              % (k, r["top"], r["h"], r["w"], f"{r['mt']}/{r['mb']}",
                 f"{r['pt']}/{r['pb']}", r["disp"], gap))
        prev = r["top"] + r["h"]

    sb, lp = d["boxes"]["stagebox"], d["boxes"]["loupe"]
    need = sb["h"] + (lp["top"] - sb["top"] - sb["h"]) + lp["h"]
    print(f"\n    同屏需要 {sb['h']} + {lp['top'] - sb['top'] - sb['h']} + {lp['h']} = "
          f"{need} px / 视口 {d['vh']} px → " +
          (f"余 {d['vh'] - need} px" if need <= d["vh"] else f"差 {need - d['vh']} px"))


if __name__ == "__main__":
    main()
