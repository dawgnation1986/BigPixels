#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
给页面拍图 + 量交互，用来人眼和数字各过一遍（自动化测不出「好不好看」）。

    python app/qa/shots.py                       # 空态 / 载入示例 / 深色
    python app/qa/shots.py --src 图.png          # 先提交一个任务，再拍结果态和全屏态
    python app/qa/shots.py --job 7b781573c6ed    # 用已完成的任务拍
    python app/qa/shots.py --probe               # 额外量一遍放大镜的跟手程度
    python app/qa/shots.py --mobile              # 手机版面（390×844 真机视口 + 触摸模拟）
    python app/qa/shots.py --mobile --size 430,932    # 想要别的手机尺寸就自己带 --size

走 CDP（app/qa/cdp.py），所以能做静态截图做不到的两件事：
  · 真的进全屏再拍 —— 靠 JS 里 requestFullscreen，不做版式特判
  · 用真的鼠标事件横扫对照台，数取块请求、长任务、最大帧间隔

产物落在 .cache/shots/。user-data-dir 也在工作区里，不写 C 盘。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SHOTS = os.path.join(ROOT, ".cache", "shots")
PROFILE = os.path.join(ROOT, ".cache", "cdp-shots")
DEMO = os.path.join(ROOT, "web", "demo")

from app.qa.cdp import Session          # noqa: E402


def post_job(base: str, src: str, preset: str, scale: int, denoise: str,
             tile: int) -> str:
    with open(src, "rb") as f:
        data = f.read()
    q = urllib.parse.urlencode({"preset": preset, "scale": scale, "denoise": denoise,
                                "tile": tile, "name": os.path.basename(src)})
    req = urllib.request.Request(base + "/api/job?" + q, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))["id"]


def wait_job(base: str, jid: str, limit: float = 1800.0) -> dict:
    t0, last = time.time(), ""
    while time.time() - t0 < limit:
        with urllib.request.urlopen(f"{base}/api/job/{jid}", timeout=20) as r:
            st = json.loads(r.read().decode("utf-8"))
        if st["state"] in ("done", "error"):
            return st
        line = f"{st['stage']} {st.get('progress', 0)}% {st.get('elapsed', 0):.1f}s"
        if line != last:
            print("      " + line)
            last = line
        time.sleep(0.5)
    raise SystemExit("任务超时")


def wait_loupe(s: Session, timeout: float = 30.0) -> bool:
    """等到放大镜三格都真的把图画上了，不然拍出来是空的。"""
    return s.wait_for(
        "(() => {const l = document.querySelector('#loupe');"
        "if (!l || l.hidden) return false;"
        "return ['#imIn','#imBic','#imAi'].every(q => {"
        "const i = document.querySelector(q);"
        "return i && i.complete && i.naturalWidth > 0;});})()", timeout=timeout)


def hover_stage(s: Session, frac: float = 0.5, yfrac: float = 0.5, settle: bool = False):
    """把鼠标移到光台上某个相对位置。

    贴边的时候一定要往里让一个像素：getBoundingClientRect 是半开区间，
    正正好取 b.x+b.w / b.y+b.h 那个点其实落在元素外，浏览器不会派 pointermove
    过来，页面里的 S.at 就停在上一处 —— 探针会读到「假的老值」，
    误判成框框没跟过去（第一版探针就栽在这儿，四个角全是假数据）。
    settle=True 时等到 S.at 真的更新到这儿再返回，不靠 sleep 猜。"""
    b = s.box("#viewport")
    if not b:
        return None
    x = int(round(b["x"] + max(1.0, min(b["w"] - 1.0, b["w"] * frac))))
    y = int(round(b["y"] + max(1.0, min(b["h"] - 1.0, b["h"] * yfrac))))
    s.mouse(x, y)
    if settle:
        wx = round((x - b["x"]) / b["w"], 5)
        wy = round((y - b["y"]) / b["h"], 5)
        s.wait_for(f"(typeof S !== 'undefined' && S.at && "
                   f"Math.abs(S.at.x - {wx!r}) < 0.004 && Math.abs(S.at.y - {wy!r}) < 0.004)",
                   timeout=3.0)
    return b


WATCH = r"""
window.__long = []; window.__frames = [];
try { new PerformanceObserver(l => { for (const e of l.getEntries())
        window.__long.push(Math.round(e.duration)); }).observe({entryTypes: ['longtask']}); } catch (e) {}
(() => { let last = performance.now();
  (function tick(t) { window.__frames.push(Math.round(t - last)); last = t;
    window.__raf = requestAnimationFrame(tick); })(performance.now()); })();
window.__n0 = performance.getEntriesByType('resource').length;
true;
"""

# 每次导航前装一次：页面里任何没接住的异常都记下来，跑完一眼能看见
TRAP = r"""
window.__err = [];
window.addEventListener('error', e => window.__err.push(String(e.message)));
window.addEventListener('unhandledrejection',
  e => window.__err.push('promise: ' + String(e.reason && e.reason.message || e.reason)));
true;
"""

# 关键 DOM 自检：界面有没有真的搭起来、三格多大、放大镜在谁家里
CHECK = r"""
(() => {
  const q = s => document.querySelector(s);
  const t = s => ((q(s) || {}).textContent || '').trim();
  const kids = s => (q(s) ? q(s).children.length : -1);
  const w = s => (q(s) ? Math.round(q(s).clientWidth) : -1);
  const l = q('#loupe');
  return {
    engine: t('#engine').slice(0, 34),
    presets: kids('#presets'), scales: kids('#scales'), denoise: kids('#denoise'),
    /* 清晰度三档必须真的画在「5 线条要多清楚」那个分组里。踩过一次：单选框的 id
       取了 #clear，跟源图卡片上「移除」那个按钮撞名 —— radioGroup 按 id 找容器，
       找到的是那个按钮，整组控件就被渲染进卡片里，控制台里那一栏空空如也，
       页面上一个错都不报。所以这里不只看「有几档」，还要看它落在哪个分组里；
       顺带查一遍全页重复 id，这类事故的根子都在那儿。 */
    clearLevels: kids('#clarity'),
    clarityGrp: (() => {
      const e = q('#clarity');
      if (!e) return null;
      const g = e.closest('.grp');
      const eb = g && g.querySelector('.eyebrow span');
      return {kids: e.children.length, tag: e.tagName.toLowerCase(),
              grp: g ? ((eb && eb.textContent) || '').trim() : null,
              /* 认分组认 id，不认文案 —— 文案随时会改，改一个字探针就废了 */
              own: !!e.closest('#clarityGrp')};
    })(),
    /* 明暗是个开关（原样 / 提亮），同样只查「画没画出来、画在哪一组」。
       它跟清晰度是同一类事故 —— id 撞名就会被渲染进别的容器里。 */
    brightGrp: (() => {
      const e = q('#bright');
      if (!e) return null;
      const g = e.closest('.grp');
      const eb = g && g.querySelector('.eyebrow span');
      return {kids: e.children.length, tag: e.tagName.toLowerCase(),
              grp: g ? ((eb && eb.textContent) || '').trim() : null,
              own: !!e.closest('#brightGrp')};
    })(),
    dupIds: (() => {
      const seen = {}, dup = [];
      document.querySelectorAll('[id]').forEach(e => {
        if (seen[e.id]) { if (!dup.includes(e.id)) dup.push(e.id); } else { seen[e.id] = 1; }
      });
      return dup;
    })(),
    keepMode: kids('#keepMode'), keepNote: t('#keepNote').slice(0, 26),
    modelWarn: q('#modelWarn') ? !q('#modelWarn').hidden : null,
    saveNote: q('#saveNote') ? !q('#saveNote').hidden : null,
    loupeHidden: l ? l.hidden : null,
    loupeParent: l && l.parentElement
                 ? (l.parentElement.id || l.parentElement.className) : '',
    paneW: w('#paneIn'), panes: kids('#panes'),
    zoom: [...document.querySelectorAll('#zoom label')].map(x => x.textContent.trim()),
    span: q('#span') ? [+q('#span').value, t('#spanOut')] : null,
    zoomOn: (typeof S !== 'undefined') ? S.zoomOn : null,
    /* 十字框按左上角摆，坐标就是视窗左上角 —— 能直接跟 S.live.vx/vy 对上 */
    cross: (() => {
      const c = q('#cross'), vp = q('#viewport');
      if (!c || !vp || c.hidden) return null;
      const r = vp.getBoundingClientRect(), b = c.getBoundingClientRect();
      return {x: +((b.left - r.left) / r.width).toFixed(4),
              y: +((b.top - r.top) / r.height).toFixed(4),
              w: +(b.width / r.width).toFixed(4), h: +(b.height / r.height).toFixed(4)};
    })(),
    /* 一屏能不能同时装下光台和放大镜 —— 用户的原话是"鱼和熊掌不可兼得"：
       看得到光台上的框框就看不到放大镜，滚下去看放大镜又丢了光台。
       这里量的就是"光台顶到放大镜底"这段总高，跟滚动位置无关。
       全屏不算：那时放大镜就住在光台里，"间隔"是负的，这个式子没意义。 */
    fit: (() => {
      const st = q('.stagebox'), lp = q('#loupe');
      if (!st || !lp || lp.hidden || document.fullscreenElement) return null;
      const a = st.getBoundingClientRect(), b = lp.getBoundingClientRect();
      const need = Math.round(a.height + (b.top - a.bottom) + b.height);
      return {stageH: Math.round(a.height), loupeH: Math.round(b.height),
              gap: Math.round(b.top - a.bottom), need: need, vh: innerHeight,
              fits: need <= innerHeight,
              free: innerHeight - need};
    })(),
    region: (typeof S !== 'undefined' && S.live) ? Math.round(S.live.V) : null,
    /* 右栏里两块的先后：放大镜必须在「这次改了什么」之前（用户要求"放大镜靠前"） */
    order: (() => {
      const a = q('#loupeSlot'), b = q('#ledgerWrap');
      if (!a || !b) return null;
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      if (ra.height === 0 || rb.height === 0) return null;   // 还没出来，量不了
      return {loupe: Math.round(ra.top + scrollY), ledger: Math.round(rb.top + scrollY),
              above: ra.top < rb.top};
    })(),
    /* 取景滑块真画出来了吗：有高度、track 也不是零宽 ——
       用户截图里这一行没出现，得能量出来才敢说是在还是不在 */
    spanBox: (() => {
      const r = q('#spanRow'), i = q('#span');
      if (!r || !i) return null;
      const rr = r.getBoundingClientRect(), ri = i.getBoundingClientRect();
      return {row: [Math.round(rr.width), Math.round(rr.height)],
              track: [Math.round(ri.width), Math.round(ri.height)],
              display: getComputedStyle(r).display};
    })(),
    eff: (typeof S !== 'undefined' && S.live) ? Math.round(S.live.z * 1000) / 1000 : null,
    tilew: q('#panes') ? q('#panes').style.getPropertyValue('--tilew').trim() : '',
    /* 滚动条：栏宽栏高都是算好的，多一个像素都算 bug ——
       全屏那一栏 overflow 是 hidden，宁可裁一点也不要有条 */
    scroll: l ? [l.clientWidth, l.scrollWidth, l.clientHeight, l.scrollHeight] : null,
    imgs: ['#imIn','#imBic','#imAi'].map(s => {
      const i = q(s); return i && i.complete && i.naturalWidth ? i.naturalWidth : 0; }),
    shown: ['#imIn','#imBic','#imAi'].map(s => {
      const i = q(s); return i ? Math.round(i.width) : 0; }),
    fs: !!document.fullscreenElement,
    /* 真机验收要看的三样：视口到底多宽（Chrome 会把窗口卡在 500，不覆盖就量不到 390）、
       pointer:coarse 有没有命中（触屏文案靠它切）、横向有没有被撑出滚动条。 */
    vw: innerWidth, vh: innerHeight,
    coarse: matchMedia('(pointer:coarse)').matches,
    hScroll: document.documentElement.scrollWidth > innerWidth + 1,
    docW: document.documentElement.scrollWidth,
    dropHint: t('#drop small'),
    err: window.__err || []
  };
})()
"""


def report_check(c: dict, mobile: bool = False):
    err = c.get("err") or []
    print("      检查：引擎 " + (c.get("engine") or "—")
          + f" · 预设 {c['presets']} 档 · 倍率 {c['scales']} 档 · 降噪 {c['denoise']} 档"
          + f" · 结果去向 {c['keepMode']} 项")
    if c.get("vw"):
        print(f"            视口 {c['vw']}×{c['vh']}"
              f" · pointer:coarse {'命中（触屏文案）' if c['coarse'] else '未命中（鼠标文案）'}"
              + (f"   ⚠ 横向撑到 {c['docW']} px，出横向滚动条了" if c["hScroll"] else "   无横向滚动"))
    if c.get("dropHint"):
        print("            选图提示：" + c["dropHint"])
    cg = c.get("clarityGrp") or {}
    print(f"            清晰度 {cg.get('kids')} 档 · 落在「{cg.get('grp')}」分组"
          + ("" if c.get("dupIds") else " · 全页无重复 id"))
    bg = c.get("brightGrp") or {}
    print(f"            明暗 {bg.get('kids')} 档 · 落在「{bg.get('grp')}」分组")
    if c.get("keepNote"):
        print("            " + c["keepNote"])
    if c.get("modelWarn"):
        print("            ⚠ 页面报了模型缺失（预期：全都在，不该出现）")
    if c.get("loupeHidden") is False:
        print(f"            放大镜：{c['panes']} 格 · 一格 {c['paneW']} px · "
              f"挂载在 <{c['loupeParent']}>")
        print(f"            倍率档位 {' '.join(c['zoom'])}"
              f" · 当前看 {c['region']} 个输出像素 · 实际倍率 {c['eff']}")
        if c.get("span"):
            print(f"            取景滑块 {c['span'][0]}/1000 → {c['span'][1]}"
                  f" · 选中的档位 {c['zoomOn']}")
        if c.get("cross"):
            x = c["cross"]
            print(f"            十字框（相对光台）左上 {x['x']:.3f},{x['y']:.3f}"
                  f" · 边长 {x['w']:.3f}")
        if c.get("spanBox"):
            sb = c["spanBox"]
            print(f"            取景滑块：行 {sb['row'][0]}×{sb['row'][1]}"
                  f" · 轨道 {sb['track'][0]}×{sb['track'][1]} px · display:{sb['display']}")
        if c.get("order"):
            o = c["order"]
            print(f"            右栏顺序：放大镜 @{o['loupe']} px · 计量表 @{o['ledger']} px"
                  + ("   放大镜在前 ✓" if o["above"] else "   ！！放大镜跑到计量表后面了"))
        if c.get("fit"):
            f = c["fit"]
            print(f"            同屏：光台 {f['stageH']} + 间隔 {f['gap']} + 放大镜 {f['loupeH']}"
                  f" = {f['need']} px / 视口 {f['vh']} px"
                  + (f"   余 {f['free']} px" if f["fits"] else f"   ⚠ 差 {-f['free']} px 放不下"))
        print(f"            三图交付宽 {c['imgs']} · 画到 {c['shown']} px（--tilew {c['tilew']}）")
        sc = c.get("scroll")
        if sc:
            over = sc[1] > sc[0] + 1 or sc[3] > sc[2] + 1
            print(f"            栏尺寸 {sc[0]}×{sc[2]}，内容 {sc[1]}×{sc[3]}"
                  + ("   ⚠ 出滚动条了" if over else "   不滚"))
    if c.get("saveNote") is not None:
        print(f"            去向提醒 {'显示' if c['saveNote'] else '未显示'}")
    bad = []
    if c.get("hScroll"):
        bad.append(f"横向出滚动条了（文档 {c['docW']} px > 视口 {c['vw']} px）")
    if c.get("dupIds"):
        bad.append("页面上有重复 id：" + "、".join("#" + x for x in c["dupIds"]))
    cg = c.get("clarityGrp")
    if not cg or cg.get("kids") != 3 or not cg.get("own"):
        bad.append(f"清晰度控件没画进「线条要多清楚」那一组（{cg}）")
    bg = c.get("brightGrp")
    if not bg or bg.get("kids") != 2 or not bg.get("own"):
        bad.append(f"明暗开关没画进「明暗」那一组（{bg}）")
    if c.get("loupeHidden") is False:
        # 放大镜必须排在「这次改了什么」之前；取景滑块必须真的画出来了（用户截图里没见到它）；
        # 光台和放大镜必须同屏 —— 用户的原话是「鱼和熊掌不可兼得」。
        if c.get("order") and not c["order"]["above"]:
            bad.append("放大镜没排在「这次改了什么」前面")
        sb = c.get("spanBox")
        if sb and ("none" in str(sb["display"]) or sb["track"][0] < 40 or sb["row"][1] < 8):
            bad.append(f"取景滑块没画出来（{sb}）")
        # 视口够高（≥800，含 1600×900 那种 vh≈802 的笔记本）就必须两样都在一屏里
        # —— 用户的原话是「鱼和熊掌不可兼得」；再矮的窗口本来就该滚，不硬凑。
        # 手机不算：窄屏版面是刻意的单列，光台和放大镜本来就该顺次往下排。
        f = c.get("fit")
        if f and not f["fits"] and f["vh"] >= 800 and not mobile:
            bad.append(f"光台和放大镜不在同一屏（差 {-f['free']} px）")
    if err:
        print("      ! JS 报错 " + str(len(err)) + " 条：")
        for e in err[:6]:
            print("          " + e[:150])
    if bad:
        print("      ! " + "；".join(bad))
    return not err and not bad

READ = r"""
window.__read = function (moves, seconds) {
  cancelAnimationFrame(window.__raf);
  const fresh = performance.getEntriesByType('resource').slice(window.__n0);
  const det = fresh.filter(r => r.name.includes('/detail'));
  const lat = det.map(r => Math.round(r.duration)).sort((a, b) => a - b);
  const f = window.__frames.slice(3).sort((a, b) => a - b);
  const at = k => f[Math.min(f.length - 1, Math.floor(f.length * k))] || 0;
  const la = k => lat[Math.min(lat.length - 1, Math.floor(lat.length * k))] || 0;
  return {
    moves: moves, seconds: seconds,
    detail: det.length, other: fresh.length - det.length,
    long: window.__long.length, longMax: window.__long.length ? Math.max.apply(null, window.__long) : 0,
    frameN: f.length, frameP50: at(.5), frameP95: at(.95), frameMax: f[f.length - 1] || 0,
    latP50: la(.5), latP95: la(.95), latMax: lat[lat.length - 1] || 0
  };
};
true;
"""


def probe_hover(s: Session) -> dict:
    """横扫对照台：量放大镜跟不跟得上鼠标。"""
    s.js(WATCH)
    b = s.box("#viewport")
    n = 48
    t0 = time.time()
    for i in range(n + 1):
        x = int(b["x"] + b["w"] * (0.06 + 0.88 * i / n))
        y = int(b["y"] + b["h"] * (0.5 + 0.16 * ((i % 5) - 2) / 2))
        s.mouse(x, y)
    dt = time.time() - t0
    time.sleep(0.5)
    return s.js(READ + "window.__read(%d, %s)" % (n, json.dumps(round(dt, 3))))


def probe_drag(s: Session) -> dict:
    """按住分割线来回拖：这是用户抱怨「这块操作很卡」的那个动作。"""
    s.js(WATCH)
    b, g = s.box("#viewport"), s.box("#grip")
    n = 48
    gy = int(g["y"] + g["h"] / 2)
    s.mouse(int(g["x"] + g["w"] / 2), gy, kind="mousePressed", button="left",
            buttons=1, clicks=1)
    t0 = time.time()
    for i in range(n + 1):
        x = int(b["x"] + b["w"] * (0.12 + 0.76 * (i / n)))
        s.mouse(x, gy, kind="mouseMoved", button="left", buttons=1)
    dt = time.time() - t0
    s.mouse(int(b["x"] + b["w"] * 0.88), gy, kind="mouseReleased", button="left")
    time.sleep(0.4)
    r = s.js(READ + "window.__read(%d, %s)" % (n, json.dumps(round(dt, 3))))
    r["split_before"] = 50
    r["split_after"] = s.js("Math.round(S.split)")
    return r


def report_probe(label: str, r: dict):
    print(f"\n  {label}：{r['moves']} 次事件，用了 {r['seconds']} s")
    print(f"    取块请求 /detail      {r['detail']} 次   其他 {r['other']} 次"
          + (f"   延迟 中位 {r['latP50']} / p95 {r['latP95']} / 最大 {r['latMax']} ms"
             if r["detail"] else ""))
    print(f"    长任务                {r['long']} 个   最长 {r['longMax']} ms")
    print(f"    帧间隔 中位 {r['frameP50']} ms   p95 {r['frameP95']} ms   最大 {r['frameMax']} ms"
          f"   （共 {r['frameN']} 帧）")
    if "split_after" in r:
        print(f"    分割线 {r['split_before']}% → {r['split_after']}%")


FRAME = r"""
(() => {
  const g = S.live, c = document.querySelector('#cross'), vp = document.querySelector('#viewport');
  if (!g || !c || c.hidden) return null;
  const r = vp.getBoundingClientRect(), b = c.getBoundingClientRect();
  return {
    at: [+S.at.x.toFixed(4), +S.at.y.toFixed(4)],
    vx: Math.round(g.vx), vy: Math.round(g.vy), V: Math.round(g.V), W: g.W, H: g.H,
    /* 框左上角在光台里的相对位置。负数 = 框伸到画面外了，被光台裁掉一截 —— 那就是对的：
       视窗是真的伸出去了，不是硬把它拽回图里。 */
    bx: +((b.left - r.left) / r.width).toFixed(4),
    by: +((b.top - r.top) / r.height).toFixed(4),
    bw: +(b.width / r.width).toFixed(4),
    bh: +(b.height / r.height).toFixed(4)
  };
})()
"""

# 鼠标推到边上时，框框该跟到边上。留 0.6% 容差给百分比取整和亚像素。
EDGE = 0.006


def probe_frame(s: Session) -> list:
    """把鼠标推到光台的四条边和四个角，逐个量框框的位置。

    这条是冲着用户报的毛病来的：「鼠标都到原图文字了，框框没到」「卡这里过不去了」。
    起因是视窗中心被夹进图内，鼠标到边上时框最多只能走到离边 V/2 的地方 —— 差一截。
    现在中心钉在鼠标底下、视窗允许伸到图外，框就该一路贴到边上。"""
    out = []
    spots = [("左上角", 0.0, 0.0), ("上边中", 0.5, 0.0), ("右上角", 1.0, 0.0),
             ("右边中", 1.0, 0.5), ("右下角", 1.0, 1.0), ("下边中", 0.5, 1.0),
             ("左下角", 0.0, 1.0), ("左边中", 0.0, 0.5), ("正中间", 0.5, 0.5)]
    for label, fx, fy in spots:
        hover_stage(s, fx, fy, settle=True)
        time.sleep(0.12)
        d = s.js(FRAME)
        if d:
            d["where"] = label
            out.append(d)
    return out


def report_frame(rows: list) -> bool:
    print("\n  框框跟手：鼠标推到光台边角，框框该一路跟过去，而不是停在半路")
    print("    位置       鼠标(x,y)      框左上      框右下     视窗左上(输出px)  边长")
    bad = []
    for d in rows:
        rx, ry = d["bx"] + d["bw"], d["by"] + d["bh"]
        # 框左上角必须就是视窗左上角（vx/W, vy/H）—— 对不上的话框根本没框住放大镜在看的那一块
        match = abs(d["bx"] - d["vx"] / d["W"]) <= EDGE and abs(d["by"] - d["vy"] / d["H"]) <= EDGE
        reach = {
            "左上角": d["bx"] <= EDGE and d["by"] <= EDGE,
            "上边中": d["by"] <= EDGE,
            "右上角": rx >= 1 - EDGE and d["by"] <= EDGE,
            "右边中": rx >= 1 - EDGE,
            "右下角": rx >= 1 - EDGE and ry >= 1 - EDGE,
            "下边中": ry >= 1 - EDGE,
            "左下角": d["bx"] <= EDGE and ry >= 1 - EDGE,
            "左边中": d["bx"] <= EDGE,
            "正中间": abs(d["bx"] - (0.5 - d["bw"] / 2)) <= EDGE,
        }[d["where"]]
        good = match and reach
        if not good:
            bad.append(d["where"])
        print("    %-8s  %5.2f,%5.2f  %6.3f,%6.3f  %6.3f,%6.3f   %7d,%7d  %6d   %s"
              % (d["where"], d["at"][0], d["at"][1], d["bx"], d["by"], rx, ry,
                 d["vx"], d["vy"], d["V"],
                 "OK" if good else ("框没贴边" if not reach else "框没框住视窗")))
    if bad:
        print("    ！贴边没做到：" + "、".join(bad))
    else:
        print("    九个位置全部贴边、且框住的正是放大镜在看的那一块")
    return not bad


def probe_slider(s: Session) -> dict:
    """拖取景滑块：倍率该连续变，标签跟着变，档位键跟着灭；
    再反过来按一个档位键，滑块该自己挪过去。"""
    rows = []
    for val in (0, 250, 500, 750, 1000):
        s.js(f"""(() => {{ const el = document.querySelector('#span');
          el.value = {val}; el.dispatchEvent(new Event('input', {{bubbles: true}})); }})()""")
        time.sleep(0.35)
        hover_stage(s, 0.5, 0.5)
        time.sleep(0.3)
        rows.append(s.js(SLIDER))
    # 反向：按一个档位键，滑块和标签都得跟着动。
    # 挑的是「倒数第二个」而不是「第二个」：键是按这张图实际能做到的倍率筛出来的，
    # 少的话只剩两档（小图就是这样，只出 1:1 和 2:1），第二个正好是最右边那个，
    # 而最右那档映射到滑块端点 1000 —— 上一步刚把滑块拖到 1000，于是"按了没挪窝"。
    # 那是探针自己挑错了键，不是界面的毛病。跳过最右一档，剩下的按哪档都会动。
    rev = s.js("""(() => {
      const labs = document.querySelectorAll('#zoom label');
      const lab = labs[Math.max(0, labs.length - 2)];
      if (!lab) return null;
      const before = +document.querySelector('#span').value;
      lab.querySelector('input').click();
      return {key: lab.textContent.trim(), before};
    })()""")
    time.sleep(0.4)
    hover_stage(s, 0.5, 0.5)
    time.sleep(0.3)
    if rev is not None:
        rev.update(s.js(SLIDER))
    return {"rows": rows, "rev": rev}


SLIDER = r"""
(() => ({
  pos: +document.querySelector('#span').value,
  label: document.querySelector('#spanOut').textContent.trim(),
  zoom: +S.zoom.toFixed(4),
  eff: S.live ? +S.live.z.toFixed(4) : null,
  V: S.live ? Math.round(S.live.V) : null,
  on: S.zoomOn,
  lit: [...document.querySelectorAll('#zoom label.on')].map(x => x.textContent.trim())
}))()
"""


def report_slider(r: dict) -> bool:
    rows, rev = r["rows"], r["rev"]
    print("\n  取景滑块：拖到哪儿倍率就该变到哪儿，五个档位键跟着同步")
    print("    滑块   实际倍率   看多少输出像素   标称     选中档位   亮着的档位键")
    good = True
    effs = [x["eff"] for x in rows]
    for x in rows:
        print("    %4d   %8.4f   %13s   %-6s   %-9s   %s"
              % (x["pos"], x["eff"] if x["eff"] is not None else -1,
                 x["V"], x["label"], "无" if x["on"] is None else x["on"],
                 " ".join(x["lit"]) or "—"))
    if any(e is None for e in effs):
        print("    ！有一步没量到实际倍率")
        good = False
    elif not all(a < b for a, b in zip(effs, effs[1:])):
        print("    ！倍率不是单调递增的 —— 拖到右边反而看得更多")
        good = False
    elif effs[0] >= effs[-1]:
        print("    ！滑块两端倍率一样，等于拖了没反应")
        good = False
    if any(x["on"] is not None for x in rows) or any(x["lit"] for x in rows):
        print("    ！拖过滑块还亮着档位键：滑块和档位该互斥（滑块落在两档之间）")
        good = False
    if rev is None:
        print("    ！没找到档位键，反向同步没测到")
        good = False
    else:
        same = "before" in rev and rev["pos"] == rev["before"]
        moved = ("滑块从 %s 挪到 %s" % (rev["before"], rev["pos"])
                 if "before" in rev else "滑块 %s/1000" % rev["pos"])
        print("    反向：按档位「%s」→ %s，读数 %s%s"
              % (rev["key"], moved, rev["label"],
                 "" if (rev["on"] is not None and rev["lit"]) else "   ！没同步上"))
        if rev["on"] is None or not rev["lit"]:
            good = False
        if same:
            # 判据是「挪了没有」，**不是**「落没落在中间」。
            # 小图的档位键本来就只剩两档，一左一右正好各占一个端点 ——
            # 实测 160×120 那张：1:1 → 位置 0，2:1 → 位置 1000，根本不存在中间值。
            # 拿"必须是中间值"当判据，会把这台机器上的正常行为判成失败（踩过）。
            print("    ！按了档位键滑块却没挪窝（还停在 %s）" % rev["pos"])
            good = False
    return good


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--job", default=None, help="已完成的任务号")
    ap.add_argument("--src", default=None, help="没有现成任务就先用这张图跑一个")
    ap.add_argument("--demo", default="lineart")
    ap.add_argument("--preset", default="art")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--denoise", default="medium")
    ap.add_argument("--tile", type=int, default=256)
    ap.add_argument("--size", default=None,
                    help="视口尺寸 宽,高。不给时：普通模式 1440,1180，--mobile 模式 390,844。")
    ap.add_argument("--fs-size", default="1920,1040",
                    help="全屏那张按这个视口尺寸拍。CDP 拉起来的浏览器进不了真全屏，"
                         "会掉到 800×600，量出来的格子尺寸是假的 —— 所以进全屏后用"
                         "设备指标覆盖成真实屏幕的尺寸再量。传 0 就不覆盖。")
    ap.add_argument("--probe", action="store_true", help="额外量一遍放大镜跟手程度")
    ap.add_argument("--mobile", action="store_true",
                    help="按真机视口渲染（设备指标覆盖 + 触摸模拟），用来验收手机版面。"
                         "不加这个的话 Chrome 把窗口宽度卡在 500，--size 390 根本渲染不出来，"
                         "而且 pointer:coarse 不会命中、触屏文案的量法就是假的。")
    a = ap.parse_args()
    base = f"http://{a.host}:{a.port}/"
    # --mobile 单开时必须落到真手机宽度：默认 1440 会让「手机版面」这一条其实在测桌面，
    # 一跑就绿，反而骗过自己。要别的尺寸就自己带 --size。
    if a.size:
        W, H = (int(v) for v in a.size.split(","))
    elif a.mobile:
        W, H = 390, 844
    else:
        W, H = 1440, 1180

    os.makedirs(SHOTS, exist_ok=True)
    # 不清空 profile：一是没必要，二是整目录删会被安全护栏拦下来。
    # 主题不靠 localStorage 记（--theme / ?theme= 参数优先），所以复用不影响出图。
    os.makedirs(PROFILE, exist_ok=True)

    jid = a.job
    if not jid and a.src:
        print(f"  ..  先跑一个任务：{a.src}")
        jid = post_job(base, a.src, a.preset, a.scale, a.denoise, a.tile)
        st = wait_job(base, jid)
        print(f"      任务 {jid} {st['state']} · {st['in_w']}×{st['in_h']} → "
              f"{st['out_w']}×{st['out_h']} · {st['elapsed']:.1f}s")

    # 每条 URL 都写死主题：profile 是复用的，而页面会把 ?theme= 记进 localStorage，
    # 不写死的话「结果态」会莫名其妙跟着上一轮的深色走。
    plan = [("01-空态", base + "?theme=light", "载入前：控制台四步 + 示例图", False),
            ("02-载入示例", base + "?theme=light&demo=" + a.demo,
             "载入示例后的工作态", False)]
    if jid:
        plan += [("03-结果", base + "?theme=light&job=" + jid,
                  "对照台 / 计量表", True),
                 ("03b-放大镜", base + "?theme=light&job=" + jid,
                  "放大镜（排在「这次改了什么」之前）", True),
                 ("04-全屏", base + "?theme=light&job=" + jid,
                  "真·requestFullscreen 之后 + 右侧实时细节栏", True)]
    plan.append(("05-深色", base + (("?theme=dark&job=" + jid) if jid else "?theme=dark"),
                 "深色主题", bool(jid)))

    report = {}
    errors = []
    with Session(base, size=(W, H), profile=PROFILE) as s:
        if not s.ready():
            raise SystemExit("页面没起来，检查服务是不是还在跑")
        if a.mobile:
            s.call("Emulation.setDeviceMetricsOverride", width=W, height=H,
                   deviceScaleFactor=1, mobile=True)
            s.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
            time.sleep(0.5)
            real = (s.js("innerWidth"), s.js("innerHeight"),
                    s.js("matchMedia('(pointer:coarse)').matches"))
            print(f"  ..  真机模式：视口 {real[0]}×{real[1]}"
                  f" · pointer:coarse {'命中' if real[2] else '未命中'}"
                  + ("" if real[0] == W else f"   ⚠ 没能覆盖成 {W} px，量的还是 {real[0]} px"))
        s.call("Page.addScriptToEvaluateOnNewDocument", source=TRAP)
        for name, url, note, with_loupe in plan:
            print(f"  ..  {name}  {note}")
            s.call("Page.navigate", url=url)
            time.sleep(0.6)
            s.ready()
            if with_loupe:
                hover_stage(s, 0.5, 0.5)
                if not wait_loupe(s):
                    print("      ! 放大镜三格没画全，这张图可能不完整")
                time.sleep(0.4)
            if name == "03b-放大镜":
                s.js("document.querySelector('#loupe')"
                     ".scrollIntoView({block:'center'})")
                time.sleep(0.6)
                hover_stage(s, 0.42, 0.5)
                time.sleep(0.4)
            if name == "04-全屏":
                s.js("document.querySelector('.stagebox').requestFullscreen()", gesture=True)
                time.sleep(1.0)
                fs = s.js("!!document.fullscreenElement")
                print(f"      fullscreenElement = {fs}")
                if not fs:
                    print("      ! 没进全屏，这张等于普通视图")
                if a.fs_size and a.fs_size != "0":
                    fw, fh = (int(v) for v in a.fs_size.split(","))
                    real = (s.js("innerWidth"), s.js("innerHeight"))
                    s.call("Emulation.setDeviceMetricsOverride", width=fw, height=fh,
                           deviceScaleFactor=1, mobile=a.mobile)
                    print(f"      视口 {real[0]}×{real[1]} 覆盖成 {fw}×{fh}"
                          f" —— 不覆盖的话量出来的格子尺寸是假的")
                    time.sleep(0.5)
                hover_stage(s, 0.5, 0.5)
                time.sleep(0.5)
            c = s.js(CHECK)
            if not report_check(c, a.mobile):
                errors.append(name)
            out = os.path.join(SHOTS, name.split("-")[0] + ".png")
            ok = s.shot(out)
            print(f"      -> {out}  {os.path.getsize(out) / 1024:.0f} KB"
                  if ok else "      ! 截图失败")
            if name == "04-全屏":
                s.js("document.exitFullscreen && document.exitFullscreen()")
                time.sleep(0.6)
                if a.fs_size and a.fs_size != "0":
                    if a.mobile:      # 真机模式下别把基础视口一起清了，后面还有一张深色要拍
                        s.call("Emulation.setDeviceMetricsOverride", width=W, height=H,
                               deviceScaleFactor=1, mobile=True)
                    else:
                        s.call("Emulation.clearDeviceMetricsOverride")
                    time.sleep(0.3)

        if a.probe:
            if not jid:
                raise SystemExit("--probe 得先有一个任务（用 --src 或 --job）")
            s.call("Page.navigate", url=base + "?job=" + jid)
            s.ready()
            hover_stage(s, 0.5, 0.5)
            wait_loupe(s)
            if not report_check(s.js(CHECK), a.mobile):
                errors.append("probe")
            stage = s.js("(() => {const i = document.querySelector('#imgAfter');"
                         "return {nat: [i.naturalWidth, i.naturalHeight],"
                         " shown: [Math.round(i.clientWidth), Math.round(i.clientHeight)]};})()")
            print(f"\n  光台上挂的图 {stage['nat'][0]}×{stage['nat'][1]}，"
                  f"显示成 {stage['shown'][0]}×{stage['shown'][1]}")
            print(f"  放大镜档位 {s.js('S.zoom')} → 实际倍率 {s.js('S.live && S.live.z')}"
                  f" · 当前区块 {s.js('JSON.stringify(S.view)')}")
            print(f"  落点默认值：滑块 {s.js('+document.querySelector(\"#span\").value')}/1000"
                  f" · 读数 {s.js('document.querySelector(\"#spanOut\").textContent')}"
                  f" · 命中的档位 {s.js('S.zoomOn')}"
                  f" · 滑块两端 {s.js('JSON.stringify(zoomRange(S.result))')}")

            r = probe_hover(s)
            report["hover"] = r
            report_probe("横扫对照台（放大镜跟着鼠标）", r)

            r2 = probe_drag(s)
            report["drag"] = r2
            report_probe("按住分割线来回拖（用户说的那一下）", r2)

            rows = probe_frame(s)
            report["frame"] = rows
            if not report_frame(rows):
                errors.append("probe-框框跟手")

            sl = probe_slider(s)
            report["slider"] = sl
            if not report_slider(sl):
                errors.append("probe-取景滑块")

    if report:
        with open(os.path.join(SHOTS, "probe.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n产物目录：" + SHOTS)
    if errors:
        # 这里装的是「页面自检没过」和「探针断言没过」两类，不全是 JS 报错 ——
        # 以前一律打成"有 JS 报错的页面"，查起来会跑偏（找了一圈报错，其实是断言在抗议）。
        print("！这些没过（页面自检 / 探针断言）：" + "、".join(errors))
        return 2
    print("页面自检：全程没有 JS 报错，探针断言也都过了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
