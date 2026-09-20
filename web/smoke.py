#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
网页服务自检：起一个真任务，把新接口和指标全跑一遍。

    python web/smoke.py                 # 默认打 http://127.0.0.1:8765
    python web/smoke.py --port 9000 --preset esrgan --scale 4
    python web/smoke.py --src .cache/test/mid_input.png --scale 4
                                        # 源图够大时才会走到「预切块」那条路

断言的是接口契约和数值合理性（尺寸、指标范围、取块是否真像素），
不是「好不好看」—— 那个得人眼看。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from io import BytesIO

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "web", "demo")


def get(url: str):
    """返回 (状态码, 响应体, 头)。4xx/5xx 不抛异常，自检要能看到状态码。"""
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def jget(url: str):
    s, b, _ = get(url)
    return json.loads(b.decode("utf-8"))


def post(url: str, body: bytes = b""):
    """空 body 的 POST 也给带上 Content-Length，不然 HTTP/1.1 会让服务端等在那儿。"""
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def delete(url: str):
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--demo", default="lineart.png")
    ap.add_argument("--src", default=None, help="直接指定源图路径，不填就用 --demo")
    ap.add_argument("--preset", default="art")
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--denoise", default="none")
    ap.add_argument("--tile", type=int, default=192)
    a = ap.parse_args()
    base = f"http://{a.host}:{a.port}"

    ok, bad = [], []

    def check(cond, label, detail=""):
        (ok if cond else bad).append(f"{label}{'  ' + detail if detail else ''}")
        print(f"  {'PASS' if cond else 'FAIL'}  {label}  {detail}")

    # 1. 页面与静态资源
    st, body, hd = get(base + "/")
    check(st == 200 and b"<html" in body.lower(), "GET / 返回页面",
          f"{len(body)} bytes")
    _, manifest, _ = get(base + "/fonts/manifest.json")
    nfonts = len(json.loads(manifest.decode("utf-8")))
    check(nfonts >= 8, "字体清单", f"{nfonts} 个 face")
    st, fb, _ = get(base + "/fonts/archivo-700.woff2")
    check(st == 200 and fb[:4] == b"wOF2", "字体文件是合法 woff2",
          f"{len(fb)} bytes")
    # 用百分号编码的 ../ 才测得到服务端 —— 客户端会把明文 /../ 直接归一化掉
    st, _, _ = get(base + "/fonts/%2e%2e%2f%2e%2e%2fupscale.py")
    check(st == 404, "静态路由挡住目录穿越", f"HTTP {st}")
    st, _, _ = get(base + "/fonts/manifest.txt")
    check(st == 404, "静态路由挡住非白名单后缀", f"HTTP {st}")

    # 版式和脚本各走自己的目录。类型必须报对：每个响应都带 nosniff，
    # 报成 octet-stream 的话浏览器会直接拒收样式表（页面变裸 HTML），
    # 这种毛病在终端里看不出来，只有浏览器才会发飙 —— 所以在这里盯死。
    st, css, hd = get(base + "/css/app.css")
    check(st == 200 and "text/css" in hd.get("Content-Type", "") and len(css) > 5000,
          "GET /css/app.css", f"{len(css)} bytes  {hd.get('Content-Type')}")
    st, js, hd = get(base + "/js/app.js")
    check(st == 200 and "javascript" in hd.get("Content-Type", "") and len(js) > 5000,
          "GET /js/app.js", f"{len(js)} bytes  {hd.get('Content-Type')}")
    check(b"<style" not in body and b'href="/css/app.css"' in body,
          "页面不再内联样式", "index.html 只留骨架")
    check(not re.search(rb"<script(?![^>]*\bsrc=)", body) and b'src="/js/app.js"' in body,
          "页面不再内联脚本", "index.html 只留骨架")
    st, _, _ = get(base + "/js/%2e%2e%2fserver.py")
    check(st == 404, "js/ 目录也挡穿越", f"HTTP {st}")
    st, _, _ = get(base + "/js/app.js.map")
    check(st == 404, "js/ 只认 .js", f"HTTP {st}")

    # 2. 预设
    cfg = jget(base + "/api/presets")
    check(len(cfg["presets"]) >= 4, "预设列表", ", ".join(p["id"] for p in cfg["presets"]))
    check(all(p["ready"] for p in cfg["presets"]),
          "所有预设权重齐备",
          ", ".join(f"{p['id']}:{p['have']}/{p['total']}" for p in cfg["presets"]))
    check(len(cfg["samples"]) >= 3, "示例图", ", ".join(s["id"] for s in cfg["samples"]))
    check(isinstance(cfg["device_probe"], dict), "设备自检结论已读", str(cfg["device_probe"]))

    # 3. 真跑一个任务
    src = a.src or os.path.join(DEMO, a.demo)
    if not os.path.isfile(src):
        print(f"  ! 找不到源图 {src}，先跑 web/make_demo.py")
        return 1
    with open(src, "rb") as f:
        data = f.read()
    q = urllib.parse.urlencode({"preset": a.preset, "scale": a.scale,
                                "denoise": a.denoise, "tile": a.tile,
                                "name": os.path.basename(src)})
    req = urllib.request.Request(base + "/api/job?" + q, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        jid = json.loads(r.read().decode("utf-8"))["id"]
    print(f"  ..  任务 {jid} 已提交（{os.path.basename(src)}）")

    t0, last, st = time.time(), "", {}
    while time.time() - t0 < 600:
        st = jget(f"{base}/api/job/{jid}")
        if st["state"] == "error":
            check(False, "任务执行", st.get("msg", ""))
            return 1
        if st["state"] == "done":
            break
        line = f"{st['stage']} {st.get('progress', 0)}% {st.get('elapsed', 0):.1f}s"
        if line != last:
            print(f"      {line}")
            last = line
        time.sleep(0.3)
    else:
        check(False, "任务超时")
        return 1

    # 4. 指标是否合理
    check(st["out_w"] == st["in_w"] * a.scale and st["out_h"] == st["in_h"] * a.scale,
          "输出尺寸 == 输入 × 倍率",
          f"{st['in_w']}×{st['in_h']} -> {st['out_w']}×{st['out_h']}")
    check(st.get("passes", 0) >= 1, "串联趟数", str(st.get("passes")))
    check(0 <= st.get("rt_ssim", -1) <= 1, "回环 SSIM 在 0..1", f"{st.get('rt_ssim')}")
    check(st.get("sharp_gain") is None or st["sharp_gain"] > 0,
          "锐度增益为正", f"{st.get('sharp_gain')}×  (bicubic {st.get('sharp_bicubic')})")
    check(st.get("mps", 0) > 0, "吞吐 MP/s", f"{st.get('mps')}")
    check(st.get("has_baseline") is True, "双三次基线已生成",
          f"{st.get('bicubic_bytes')} bytes")
    check(bool(st.get("preset_label")), "预设中文名", st.get("preset_label"))
    check(st.get("t_net", 0) <= st.get("elapsed", 0), "推理耗时 <= 总耗时",
          f"{st.get('t_net')}s / {st.get('elapsed'):.1f}s")

    # 5. 三张产物都取得到，而且字节数跟记录一致
    for what, key, magic in (("input", "in_bytes", None),
                             ("baseline", "bicubic_bytes", b"\x89PNG"),
                             ("result", "out_bytes", b"\x89PNG")):
        s, b, hd = get(f"{base}/api/job/{jid}/{what}")
        check(s == 200 and len(b) == st.get(key, -1),
              f"GET /{what}", f"{len(b)} bytes == {key} {st.get(key)}")
        if magic:
            check(b[:len(magic)] == magic, f"{what} 是 PNG")

    # 6. 缩略预览：光台只碰这一张，浏览器不该被塞整幅
    pv_max = cfg["limits"]["preview_max"]
    s, vb, _ = get(f"{base}/api/job/{jid}/result?view=1")
    vim = Image.open(BytesIO(vb))
    check(s == 200 and max(vim.size) <= pv_max,
          "结果缩略预览不超过边长上限",
          f"{vim.size[0]}×{vim.size[1]} · {len(vb) / 1048576:.2f} MB")
    # 缩到整数像素必然有半个像素以内的取整误差，所以比比例、不比乘积
    r_pv, r_full = vim.size[0] / vim.size[1], st["out_w"] / st["out_h"]
    check(abs(r_pv - r_full) < 0.002,
          "预览保持原始宽高比",
          f"{vim.size} vs {st['out_w']}×{st['out_h']}（比例差 {abs(r_pv - r_full):.5f}）")
    s, _, _ = get(f"{base}/api/job/{jid}/input?view=1")
    check(s == 200, "输入缩略预览可取")

    # 7. 放大镜取块：要的是真像素，不是重采样过的缩略图
    _, fullb, _ = get(f"{base}/api/job/{jid}/result")
    full = Image.open(BytesIO(fullb)).convert("RGB")
    W, H = full.size
    x, y = W // 3, H // 3
    w, h = min(320, W - x), min(320, H - y)
    qs = f"x={x}&y={y}&w={w}&h={h}"

    s, db, _ = get(f"{base}/api/job/{jid}/detail?layer=result&{qs}")
    d = Image.open(BytesIO(db))
    diff = int(np.abs(np.asarray(d.convert("RGB"), np.int16)
                      - np.asarray(full.crop((x, y, x + w, y + h)), np.int16)).max())
    check(s == 200 and d.size == (w, h) and diff == 0,
          "detail/result 与整幅裁剪逐像素一致", f"{d.size} 最大差 {diff}")

    s, bb, _ = get(f"{base}/api/job/{jid}/detail?layer=bicubic&{qs}")
    bi = Image.open(BytesIO(bb))
    check(s == 200 and bi.size == (w, h), "detail/bicubic 尺寸与结果块一致", f"{bi.size}")

    s, ib2, _ = get(f"{base}/api/job/{jid}/detail?layer=input&{qs}")
    ii = Image.open(BytesIO(ib2))
    sr = st["out_w"] / st["in_w"]
    check(s == 200 and abs(ii.size[0] - round(w / sr)) <= 1,
          "detail/input 是原生输入像素", f"{ii.size} 期望约 {round(w / sr)}")

    s, eb, _ = get(f"{base}/api/job/{jid}/detail?layer=result&x={W - 10}&y={H - 10}&w=800&h=800")
    check(s == 200 and Image.open(BytesIO(eb)).size == (10, 10),
          "越界取块被 clamp 到边界", str(Image.open(BytesIO(eb)).size))
    for qs2, label in (("layer=result&x=0&y=0&w=99999&h=8", "边长超限"),
                       ("layer=nope&x=0&y=0&w=8&h=8", "layer 非法"),
                       ("layer=result&x=abc&y=0&w=8&h=8", "坐标非整数")):
        s, _, _ = get(f"{base}/api/job/{jid}/detail?{qs2}")
        check(s == 400, f"detail 挡住{label}", f"HTTP {s}")

    # 7b. dw = 「这一块最终在屏幕上画多宽」。放大镜倍率比 1:1 更远的时候，
    #     前端要的是一大块画面压进一格：块在这头就缩好再发，省传输也省浏览器解码。
    #     1:1 时 dw 正好等于边长，必须一个字都不动 —— 这条锁的就是「看大块不影响画质」。
    s, db2, _ = get(f"{base}/api/job/{jid}/detail?layer=result&{qs}&dw={w}")
    d2 = Image.open(BytesIO(db2)).convert("RGB")
    diff2 = int(np.abs(np.asarray(d2, np.int16)
                       - np.asarray(full.crop((x, y, x + w, y + h)), np.int16)).max())
    check(s == 200 and d2.size == (w, h) and diff2 == 0,
          "dw == 边长时逐像素不动（1:1 那档没被碰）", f"{d2.size} 最大差 {diff2}")

    dw4 = max(48, w // 4)
    box = (dw4, max(1, round(dw4 * h / w)))
    s, sb, _ = get(f"{base}/api/job/{jid}/detail?layer=result&{qs}&dw={dw4}")
    sm_ = Image.open(BytesIO(sb)).convert("RGB")
    ref = full.crop((x, y, x + w, y + h)).resize(box, Image.LANCZOS)
    ds = int(np.abs(np.asarray(sm_, np.int16) - np.asarray(ref, np.int16)).max())
    check(s == 200 and sm_.size == box and ds <= 2,
          "dw 更小时按显示尺寸缩好再发", f"{sm_.size} 与本地 LANCZOS 最大差 {ds}")

    s, cb2, _ = get(f"{base}/api/job/{jid}/detail?layer=bicubic&{qs}&dw={dw4}")
    check(s == 200 and Image.open(BytesIO(cb2)).size == box,
          "dw 对双三次基线同样生效", f"{Image.open(BytesIO(cb2)).size}")

    s, ib3, _ = get(f"{base}/api/job/{jid}/detail?layer=input&{qs}&dw={dw4}")
    ii3 = Image.open(BytesIO(ib3))
    check(s == 200 and abs(ii3.size[0] - round(w / sr)) <= 1,
          "dw 不缩原图那一格（它要的是自己的像素，放大交给前端最近邻）", f"{ii3.size}")

    s, _, _ = get(f"{base}/api/job/{jid}/detail?layer=result&x=0&y=0&w=3000&h=3000")
    check(s == 200, "detail 放行一大块（旧上限 2048，装不下放大镜最远那档）", f"HTTP {s}")
    for qs3, label in (("layer=result&x=0&y=0&w=8&h=8&dw=99999", "dw 超限"),
                       ("layer=result&x=0&y=0&w=8&h=8&dw=abc", "dw 非数字")):
        s, _, _ = get(f"{base}/api/job/{jid}/detail?{qs3}")
        check(s == 400, f"detail 挡住{label}", f"HTTP {s}")

    # 8. 预切块：输出够大才走这条路，走了就得说出来
    if st["mp"] > 4.0:
        check("result" in (st.get("tiled") or []), "大结果已预切块",
              f"{st['mp']} MP → {st.get('tiled')}")
    else:
        print(f"  SKIP  预切块（输出只有 {st['mp']} MP，没到 4 MP 的阈值）")

    # 9. 工程文件夹：一次工程一个文件夹，产物和记录都在里面
    cfg = jget(f"{base}/api/presets")
    rel = st.get("rel") or ""
    check(bool(rel), "任务带回了工程目录相对路径", rel)
    check("/" in rel and "\\" not in rel, "相对路径是正斜杠", rel)
    jd = os.path.join(ROOT, rel.replace("/", os.sep)) if rel else ""
    check(os.path.isdir(jd), "工程文件夹真的存在",
          rel + "  " + (str(sorted(os.listdir(jd))) if os.path.isdir(jd) else "不存在"))
    for fn in ("result.png", "job.json"):
        check(os.path.isfile(os.path.join(jd, fn)), f"工程文件夹里有 {fn}")
    name = os.path.basename(jd)
    check(bool(re.match(r"^\d{8}-\d{6}_.+_" + jid + r"$", name)),
          "文件夹名 = 时间戳_图名_任务号", name)
    check(os.path.isdir(os.path.join(ROOT, "outputs", "web")), "工程都收在 outputs/web 下")
    flat = [f for f in os.listdir(os.path.join(ROOT, "outputs", "web"))
            if os.path.isfile(os.path.join(ROOT, "outputs", "web", f))]
    check(not flat, "outputs/web 根下没有散文件了", str(flat[:4]))

    # 迁移是幂等的：再喊一次不该再搬东西
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import server as S                                            # noqa: E402
    again = S.migrate_layout()
    check(again["runs"] == 0, "旧版归档是幂等的（再跑一遍一无所获）", str(again))

    # 10. 模型自检 + 保存/暂存
    m = cfg.get("models") or {}
    check(m.get("ok") is True and m.get("total", 0) > 0,
          "模型自检：权重齐", f"{m.get('have')}/{m.get('total')}")
    check(bool(cfg.get("work_rel")) and bool(m.get("hint")),
          "缺权重时界面知道去哪下载", str(m.get("hint")))
    keep = (cfg.get("settings") or {}).get("keep")
    check(isinstance(keep, bool), "默认是保存模式（keep=true）", str(keep))
    disk = cfg.get("disk") or {}
    check((disk.get("used") or 0) > 0 and (disk.get("jobs") or 0) > 0,
          "报出了工程目录占用", f"{round((disk.get('used') or 0) / 1048576, 1)} MB / "
                               f"{disk.get('jobs')} 次工程")
    s, _, _ = post(f"{base}/api/settings?keep=0", b"")
    check(s == 200 and jget(f"{base}/api/settings")["keep"] is False,
          "设置能切到暂存模式")
    # 切模式不该顺手删东西 —— 用户的工程得原样在
    check(os.path.isfile(os.path.join(jd, "result.png")), "切模式没动已有工程")
    # 用临时目录验一遍清空逻辑只认自己的文件夹形状，别碰真目录
    tmp = tempfile.mkdtemp(prefix="smoke_wipe_", dir=os.path.join(ROOT, ".cache"))
    os.makedirs(os.path.join(tmp, "20260920-191016_图名_" + jid))
    os.makedirs(os.path.join(tmp, "别动我"))
    old, S.WORK_DIR = S.WORK_DIR, tmp
    wiped = S.wipe_work()
    S.WORK_DIR = old
    left = sorted(os.listdir(tmp))
    check(wiped == 1 and left == ["别动我"],
          "暂存清空只删工程文件夹，别的不碰", f"清了 {wiped} 个，留下 {left}")
    shutil.rmtree(tmp, ignore_errors=True)
    s, _, _ = post(f"{base}/api/settings?keep=1", b"")
    check(s == 200 and jget(f"{base}/api/settings")["keep"] is True,
          "能切回保存模式")
    check(os.path.isfile(os.path.join(jd, "result.png")), "整轮下来结果一步没丢")

    # 11. 放大镜看得最远那几档：一大块画面从缩略预览里取，别去瓦片里拼几千万像素。
    #     自检这张图输出才 1440（没超过预览上限 1600），走不到这条路，所以在这儿
    #     自己造一张大的：出图 3000×2000 + 预览 1600，直接算，不依赖任何任务。
    tmpv = tempfile.mkdtemp(prefix="smoke_view_", dir=os.path.join(ROOT, ".cache"))
    big = Image.new("RGB", (3000, 2000))
    bp = big.load()
    for i in range(0, 3000, 40):
        for j in range(0, 2000, 40):
            bp[i, j] = ((i * 7) % 256, (j * 5) % 256, (i + j) % 256)
    bigp = os.path.join(tmpv, "result.png")
    big.save(bigp)
    pv = big.resize((1600, 1067), Image.LANCZOS)
    pvp = os.path.join(tmpv, "preview-result.png")
    pv.save(pvp)
    fake = {"out_w": 3000, "out_h": 2000, "out": bigp, "view": pvp, "tiles": {}}
    bx, by, bw = 900, 600, 1200
    got = S.view_region(fake, bx, by, bw, bw, 300, 300)
    check(got is not None and got.size == (300, 300),
          "看得远那档从缩略预览取块", str(got.size if got is not None else None))
    if got is not None:
        k = 1600 / 3000
        refv = pv.crop((round(bx * k), round(by * k),
                        round((bx + bw) * k), round((by + bw) * k))).resize((300, 300),
                                                                              Image.LANCZOS)
        dv = int(np.abs(np.asarray(got.convert("RGB"), np.int16)
                        - np.asarray(refv, np.int16)).max())
        check(dv == 0, "预览取块与本地同法逐像素一致", f"最大差 {dv}")
    check(S.view_region(fake, 0, 0, 3000, 2000, 2400, 2400) is None,
          "预览撑不起这一格时退回原像素（宁可慢也不糊）")
    check(S.view_region(fake, bx, by, bw, bw, 600, 600) is not None,
          "预览够画这一格就照常给")
    shutil.rmtree(tmpv, ignore_errors=True)

    # 12. 收尾：自检跑出来的工程不该赖在 outputs/web 里越堆越多，顺手验一遍删除
    s, _, _ = delete(f"{base}/api/job/{jid}")
    check(s == 200 and not os.path.isdir(jd),
          "删掉这次测试工程（记录和文件夹一起走）", f"HTTP {s} · {os.path.basename(jd)}")
    s, _, _ = delete(f"{base}/api/job/{jid}")
    check(s == 404, "重复删返回 404", f"HTTP {s}")

    print(f"\n  {len(ok)} 项通过" + (f"，{len(bad)} 项失败：{bad}" if bad else "，全部通过"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
