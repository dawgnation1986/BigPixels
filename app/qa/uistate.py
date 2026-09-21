#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把两个「平时看不见」的界面状态渲染出来拍图：

  1. 暂存模式的完成提醒（真按暂存跑一次会把工程清掉，所以只在页面上把状态注进去）
  2. 缺权重时的提示条（本地权重是全的，同样只能注）

纯粹为了人眼验收，不改任何设置、不动任何文件。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SHOTS = os.path.join(ROOT, ".cache", "shots")
PROFILE = os.path.join(ROOT, ".cache", "cdp-shots")

from app.qa.cdp import Session          # noqa: E402

BASE = "http://127.0.0.1:8765/"


def main() -> int:
    jid = sys.argv[1] if len(sys.argv) > 1 else "e3c72086d0a7"
    os.makedirs(SHOTS, exist_ok=True)
    os.makedirs(PROFILE, exist_ok=True)

    with Session(BASE, size=(1440, 1180), profile=PROFILE) as s:
        if not s.ready():
            raise SystemExit("页面没起来")

        # ---- 1. 暂存模式的完成提醒 + 设置里的说明 ----
        s.call("Page.navigate", url=BASE + "?theme=light&job=" + jid)
        time.sleep(0.8)
        s.ready()
        time.sleep(1.2)
        print("暂存提醒：", s.js("""(() => {
          S.keep = false; paintKeep(); showSaveNote(S.result);
          return document.querySelector('#saveNote').textContent.trim();})()"""))
        s.js("document.querySelector('#saveNote').scrollIntoView({block:'center'})")
        time.sleep(0.5)
        s.shot(os.path.join(SHOTS, "tmp-暂存提醒.png"))

        # 设置区那一栏
        s.js("document.querySelector('details.adv').open = true")
        s.js("document.querySelector('#keepNote').scrollIntoView({block:'center'})")
        time.sleep(0.5)
        print("设置说明：", s.js("document.querySelector('#keepNote').textContent"))
        print("磁盘说明：", s.js("document.querySelector('#diskNote').textContent"))
        s.shot(os.path.join(SHOTS, "tmp-结果去向设置.png"))

        # ---- 2. 缺权重提示条 ----
        s.call("Page.navigate", url=BASE + "?theme=light")
        time.sleep(0.8)
        s.ready()
        n = s.js("""(() => {
          const c = JSON.parse(JSON.stringify(S.cfg));
          c.models = {ok: false, have: 12, total: 15, dir: c.models.dir, hint: c.models.hint,
                      missing: ['waifu2x_swin_photo_noise0_2x.onnx',
                                'waifu2x_swin_photo_noise1_2x.onnx',
                                '4x-AnimeSharp.onnx']};
          c.presets.forEach(p => { if (p.id === 'photo' || p.id === 'anime') p.ready = false; });
          renderModelWarn(c);
          return document.querySelector('#modelWarn').hidden ? 0
                 : document.querySelector('#modelWarn').textContent.trim().length;})()""")
        print("提示条字数：", n)
        time.sleep(0.4)
        s.shot(os.path.join(SHOTS, "tmp-缺权重提示.png"))

    print("产物：" + SHOTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
