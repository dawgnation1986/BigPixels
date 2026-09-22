#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BigPixels 本地版 —— 网页服务，跟 CLI 共用同一套推理引擎。

    python app/server/server.py                  # http://127.0.0.1:8765
    python app/server/server.py --port 9000

接口
    GET  /                          界面
    GET  /fonts/<file>              自托管字体（woff2）
    GET  /demo/<file>               首页示例图
    GET  /js/i18n.js                界面上那一堆静态文案（按当前语言现拼）
    GET  /api/presets               预设 / 降噪档 / 倍率 / 设备 / 示例图 / 上限 / 磁盘 / 设置 / 词条
    GET  /api/settings              当前设置（保存模式、界面语言）
    POST /api/settings?keep=1|0     改保存模式
    POST /api/settings?lang=en      改界面语言（zh / en / ja）
    POST /api/job?preset=&scale=&denoise=&tile=&name=    请求体就是图片二进制
    GET  /api/job/<id>              任务状态（进度、阶段、耗时、指标、工程目录）
    GET  /api/job/<id>/input        原图（?view=1 取缩略预览）
    GET  /api/job/<id>/baseline     双三次插值基线（同目标尺寸，用来对照）
    GET  /api/job/<id>/result       AI 放大结果（?view=1 取缩略预览）
    GET  /api/job/<id>/detail       一小块真像素，给放大镜用
                                    ?layer=input|bicubic|result&x=&y=&w=&h=
                                    x/y/w/h 是「输出像素」坐标

输出目录：一次工程一个文件夹
    outputs/web/20260920-191402_SharkGirl_e3c72086d0a7/
        input.jpg            原图（后缀随上传）
        result.png           AI 放大结果
        bicubic.png          双三次基线（输出过大时不生成，省去整幅编码的开销）
        preview-input.png    最长边 1600 的缩略预览（光台用）
        preview-result.png
        tiles/{input,result}/  1024² 瓦片（大图才切，放大镜按需读几块）
        job.json             这条任务的完整记录
    旧版本散落的 {id}.png / {id}_bicubic.png / {id}_t/ 会在启动时自动归档，
    且为无损操作：仅在确认目标文件到位之后才删除原文件。

保存模式（默认）与暂存模式
    keep=true   结果就留在上面那个文件夹里，服务不做任何自动清理。
    keep=false  结果仅暂存：条数/磁盘超限时按时间淘汰，服务停止时清空整个工程目录，
                因此界面在完成后会提示「先下载」。
    设置存在 outputs/settings.json，重启后还在。

界面语言（zh / en / ja）
    界面上给人看的每一句都在 app/locales/<lang>.json 里，代码里不写死文案 ——
    所以多一种语言 = 多一个 json 文件，不用改逻辑。当前语言跟保存模式一起记在
    outputs/settings.json：首次运行由 app/server/bootstrap.py 问一次，之后在网页的
    「更多设置」里切换，命令行也可以 --lang 指定。

三条性能约束，均来自实际踩过的坑：

  1. 别对整幅图做中间运算。双三次基线的锐度只看中心 512×512，就只插值那一小块 ——
     6744×10112 整幅插值成 float32 需 816 MB，纯属浪费。
  2. 别把大图丢给浏览器。光台用最长边 1600 px 的缩略预览，68 MP 那 34 MB PNG 只用于下载。
  3. 放大镜要的是「一小块真像素」，不是整幅位图。超过 4 MP 的图在任务结束时预切成
     1024² 的 PNG 瓦片，取块时只读命中的那几块，一次请求几十毫秒。

这三条都只碰「怎么算、怎么传」，不碰输出本身：下载到的永远是模型原生分辨率的
无损 PNG，与未做这些优化之前逐像素一致（app/qa/perf_probe.py 会当场验证）。

除「输出多大、耗时多久」之外，这里还额外计算了三项指标，并在界面上直接展示：
    rt_ssim / rt_psnr  放大结果缩回原尺寸后跟原图的相似度 —— 衡量有没有把内容改跑
    sharp_gain         结果与双三次基线的拉普拉斯方差之比 —— 衡量锐度真的涨了多少
    bicubic_bytes      同样尺寸下双三次的 PNG 体积 —— 细节换来的体积代价
"""
from __future__ import annotations

import json
import math
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
import http.server
from io import BytesIO

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

try:
    import numpy as np                  # noqa: E402
    from PIL import Image               # noqa: E402
    import app.core as U                # noqa: E402  引擎内核（app/core）
    import app.core.metrics as M        # noqa: E402  指标只此一份
except ImportError as e:                # 依赖没装齐时给一句人话，别甩 traceback
    sys.exit(f"缺依赖：{e}\n先装一遍：pip install -r requirements.txt")

WEB_DIR = os.path.join(ROOT, "web")
FONT_DIR = os.path.join(WEB_DIR, "fonts")
DEMO_DIR = os.path.join(WEB_DIR, "demo")
CSS_DIR = os.path.join(WEB_DIR, "css")      # 版式
JS_DIR = os.path.join(WEB_DIR, "js")        # 行为
# index.html 只留骨架，样式和脚本各自成文件 —— 两千行的页面里翻一段 script 太费劲。
# 这两个目录跟着 server.py 一起走，别的机器上拷过去也能直接跑。

OUT_ROOT = os.path.join(ROOT, "outputs")
WORK_DIR = os.path.join(OUT_ROOT, "web")        # 一次工程一个文件夹都在这下面
SETTINGS_PATH = U.SETTINGS_PATH                 # 唯一出处是 app/core/paths.py
os.makedirs(WORK_DIR, exist_ok=True)

MAX_UPLOAD = 40 * 1024 * 1024
MAX_BASELINE_MP = 30.0        # 超过这个像素量不再落盘双三次基线（体积对照那一项就留空）
METRIC_CROP = 512             # 锐度对比只看中心 512×512，够用且不拖慢大图
OVERLAP = 16                  # 瓦片重叠像素，跟 upscale.SRRunner 默认值保持一致

PREVIEW_MAX = 1600            # 缩略预览最长边：光台要的是构图不是像素，68 MP 交给它是灾难
TILE_PX = 1024                # 预切瓦片边长
TILE_IF_MP = 4.0              # 超过这个像素量才值得预切（小于它每次现解码也就几十毫秒）
TILE_REQ_MAX = 4096           # 单次取块的边长上限：放大镜最远那一档要的是一大块画面
TILE_REQ_MP = 17.0            # 单次取块的总像素上限（百万）。发出去之前会按 dw 缩到屏幕尺寸，
                              # 所以字节数不随它涨；这里卡的是服务端自己拼块的峰值内存。
TILE_PAD = 12                 # 基线插值往外多取一圈，避开边缘效应

MAX_JOBS_TEMP = 16            # 暂存模式：内存里最多留这么多条
DISK_TEMP = 1500 * 1024 * 1024    # 暂存模式：工程目录总占用上限，超了按时间淘汰
MAX_RESTORE = 600             # 启动时最多捡回这么多条记录，免得历史太长把启动拖慢

CT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
      ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff",
      ".tiff": "image/tiff",
      # css/js 必须报对类型：下面每个响应都带 X-Content-Type-Options: nosniff，
      # 类型不对浏览器会直接拒收样式表（页面就成了裸 HTML），不是"凑合还能用"。
      ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}

STATIC_OK = {
    "fonts": {".woff2", ".json"},
    "demo": {".png", ".jpg", ".jpeg", ".webp"},
    "css": {".css"},
    "js": {".js"},
}

# 这些字段是服务端本地路径，不往 JSON 里吐（rel 是给人看的相对路径，可以吐）
INTERNAL = ("src", "out", "baseline", "view", "view_in", "tiles", "dir")

JID_RE = re.compile(r"^[0-9a-f]{12}$")
# 工程文件夹名：时间戳_名字_任务号。末尾那 12 位十六进制是任务号，
# 一眼就能认出是我们自己生成的文件夹 —— 清空暂存目录时只认这个形状。
JOB_DIR_RE = re.compile(r"^\d{8}-\d{6}_.{1,64}_[0-9a-f]{12}$")

# 老版本的散文件：{id}.png / {id}_in.jpg / {id}_bicubic.png / {id}_view.png / {id}_in_view.png
LEGACY_RE = re.compile(r"^(?P<jid>[0-9a-f]{12})"
                       r"(?P<kind>_in_view|_bicubic|_view|_in)?"
                       r"(?P<ext>\.[A-Za-z0-9]{2,5})?$")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
TASK_Q: "queue.Queue[str]" = queue.Queue()
TILE_LOCKS: dict[str, threading.Lock] = {}

# 设置（保存模式 / 界面语言）只有一份，落在 outputs/settings.json，
# 读写都走 app/core/settings.py —— 服务端不再自己开一套，免得两边互相覆盖。
CFG = U.settings.load()


def now() -> float:
    return time.time()


def clamp(v, a, b):
    return min(b, max(a, v))


def mp_of(w: int, h: int) -> float:
    return round(w * h / 1e6, 2)


def dir_size(p: str, cap: int = 0) -> int:
    """目录总字节数。cap > 0 时估到大概就收手，省得几万个瓦片数半天。"""
    tot = 0
    for base, _dirs, files in os.walk(p):
        for fn in files:
            try:
                tot += os.path.getsize(os.path.join(base, fn))
            except OSError:
                pass
        if cap and tot > cap:
            break
    return tot


def rm_tree(p: str) -> None:
    """删掉一整棵目录。

    不用 shutil.rmtree —— 这里只删我们自己生成的工程文件夹，
    路径形态已经被 JOB_DIR_RE 卡死过一道，出圈就直接不动手。
    """
    if not os.path.isdir(p):
        return
    for base, _dirs, files in os.walk(p, topdown=False):
        for fn in files:
            try:
                os.remove(os.path.join(base, fn))
            except OSError:
                pass
    for base, _dirs, _files in os.walk(p, topdown=False):
        try:
            os.rmdir(base)
        except OSError:
            pass


# ------------------------------------------------------------------ 设置 -- #
def load_settings() -> dict:
    return U.settings.load()


def save_settings() -> None:
    U.settings.save(CFG)


# ------------------------------------------------------- 工程文件夹与记录 -- #
def slug_of(name: str, n: int = 28) -> str:
    """把上传的文件名压成一段能塞进文件夹名的短标签，中文照留 —— 认得出是哪张图。"""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    s = re.sub(r'[\\/:*?"<>|\s]+', "-", stem).strip("-. ")
    s = s[:n].strip("-. ")
    return s or "upload"


def job_dir_name(jid: str, t: float, name: str) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(t))}_{slug_of(name)}_{jid}"


def meta_path(j: dict) -> str:
    return os.path.join(j["dir"], "job.json")


def save_meta(j: dict) -> None:
    """把任务记录写进工程文件夹。

    不然服务一重启，内存里的 JOBS 就没了 —— 产物还躺在 outputs/web 里，
    但 ?job=<id> 直接 404，那条链接就白发了。
    """
    try:
        tmp = meta_path(j) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(j, f, ensure_ascii=False)
        os.replace(tmp, meta_path(j))
    except OSError:
        pass


def job_json(j: dict) -> dict:
    """给界面看的那一份：剔掉本地绝对路径，但把 rel（相对项目根的工程目录）留下 ——
    界面要告诉人「结果存哪了」，靠的就是它。"""
    out = {k: v for k, v in j.items() if k not in INTERNAL}
    if out.get("rel"):
        out["rel"] = str(out["rel"]).replace("\\", "/")
    return out


def adopt(j: dict) -> dict:
    """把一条从盘上捡回来的记录修好：路径失效的字段清掉，补上推导出来的字段。"""
    d = j["dir"]
    j["rel"] = os.path.relpath(d, ROOT)
    for k in ("src", "out", "baseline", "view", "view_in"):
        if j.get(k) and not os.path.isfile(j[k]):
            j[k] = None
    tiles = {k: v for k, v in (j.get("tiles") or {}).items()
             if isinstance(v, str) and os.path.isdir(v)}
    j["tiles"] = tiles
    j["tiled"] = sorted(tiles)
    for k in ("in_w", "in_h", "out_w", "out_h"):
        if j.get(k) is not None:
            j[k] = int(j[k])
    if j.get("out_w") and j.get("in_w"):
        j["mp"] = mp_of(j["out_w"], j["out_h"])
    if j.get("out") and os.path.isfile(j["out"]):
        j["out_bytes"] = os.path.getsize(j["out"])
    if j.get("src") and os.path.isfile(j["src"]):
        j["in_bytes"] = os.path.getsize(j["src"])
    if j.get("baseline") and os.path.isfile(j["baseline"]):
        j["bicubic_bytes"] = os.path.getsize(j["baseline"])
        j["has_baseline"] = True
    else:
        j["has_baseline"] = False
    return j


def restore_jobs() -> int:
    """启动时把上一轮的工程捡回来，结果文件已经不在了的就把文件夹丢掉。"""
    n = 0
    try:
        entries = sorted((e for e in os.listdir(WORK_DIR)
                          if os.path.isdir(os.path.join(WORK_DIR, e))), reverse=True)
    except OSError:
        return 0
    for fn in entries[:MAX_RESTORE]:
        if not JOB_DIR_RE.match(fn):
            continue
        d = os.path.join(WORK_DIR, fn)
        mp_ = os.path.join(d, "job.json")
        if not os.path.isfile(mp_):
            continue
        try:
            with open(mp_, encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue
        jid = j.get("id")
        if not isinstance(jid, str) or not JID_RE.match(jid) or j.get("state") != "done":
            continue
        j["dir"] = d
        adopt(j)
        if not j.get("out"):
            rm_tree(d)                       # 记录在、结果没了：这个文件夹没有留着的意思
            continue
        j.setdefault("queued", os.path.getmtime(mp_))
        j["keep"] = bool(CFG["keep"])
        JOBS[jid] = j
        n += 1
    return n


def drop_job(j: dict) -> None:
    d = j.get("dir") or ""
    if os.path.isdir(d) and JOB_DIR_RE.match(os.path.basename(d)):
        rm_tree(d)


def wipe_work() -> int:
    """暂存模式退出时清空工程目录。只认我们自己的文件夹形状。"""
    n = 0
    try:
        entries = os.listdir(WORK_DIR)
    except OSError:
        return 0
    for fn in entries:
        p = os.path.join(WORK_DIR, fn)
        if os.path.isdir(p) and JOB_DIR_RE.match(fn):
            rm_tree(p)
            n += 1
    return n


# --------------------------------------------------------------- 旧版归档 -- #
def migrate_layout() -> dict:
    """
    老版本把产物直接铺在 outputs/web 根下（{id}.png / {id}_in.jpg / {id}_t/…），
    几十次跑下来就是一团。这里按任务号归堆，每次工程收进一个文件夹。

    幂等：跑第二遍已经没有散文件可收了，直接返回 0。
    无损：先把新位置写到位，再删旧文件；中途出错只会留下没删的旧文件，不会丢内容。
    """
    stats = {"runs": 0, "files": 0, "leftover": 0, "failed": 0}
    try:
        names = os.listdir(WORK_DIR)
    except OSError:
        return stats

    groups: dict[str, dict] = {}
    for fn in names:
        p = os.path.join(WORK_DIR, fn)
        if os.path.isdir(p):
            m = re.match(r"^([0-9a-f]{12})_t$", fn)
            if m:
                groups.setdefault(m.group(1), {})["tiles"] = p
            continue
        m = LEGACY_RE.match(fn)
        if not m:
            continue
        g = groups.setdefault(m.group("jid"), {})
        kind = m.group("kind")
        if kind == "_in":
            g["in"] = p
        elif kind == "_bicubic":
            g["bicubic"] = p
        elif kind == "_view":
            g["view"] = p
        elif kind == "_in_view":
            g["view_in"] = p
        elif fn.endswith(".json"):
            g["meta"] = p
        elif fn.endswith(".png"):
            g["out"] = p

    for jid, g in groups.items():
        if not any(g.get(k) for k in ("in", "out", "bicubic", "view", "view_in", "meta", "tiles")):
            continue                    # 只匹配上名字、实际没东西可搬，不建空文件夹
        meta = {}
        if g.get("meta"):
            try:
                with open(g["meta"], encoding="utf-8") as f:
                    meta = json.load(f) or {}
            except Exception:
                meta = {}
        t = meta.get("queued")
        if not isinstance(t, (int, float)):
            stamp_src = g.get("out") or g.get("in") or g.get("meta")
            t = os.path.getmtime(stamp_src) if stamp_src and os.path.exists(stamp_src) else now()
        name = meta.get("name") or "legacy"
        d = os.path.join(WORK_DIR, job_dir_name(jid, t, name))
        if os.path.exists(d):
            continue
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            stats["failed"] += 1
            continue

        # 显式列「旧位置 → 新位置」，不玩键名映射 —— 认错了就是把人的结果搬丢
        ext = os.path.splitext(g.get("in") or "")[1].lower()
        plan = [("in", "input" + (ext if ext in CT else ".png")),
                ("out", "result.png"),
                ("bicubic", "bicubic.png"),
                ("view", "preview-result.png"),
                ("view_in", "preview-input.png")]
        moved: dict[str, str] = {}
        try:
            for key, fname in plan:
                if not g.get(key):
                    continue
                dst = os.path.join(d, fname)
                os.replace(g[key], dst)
                moved[key] = dst
        except OSError:
            rm_tree(d)
            stats["failed"] += 1
            continue
        if g.get("tiles") and os.path.isdir(g["tiles"]):
            try:
                os.replace(g["tiles"], os.path.join(d, "tiles"))
            except OSError:
                pass
        if g.get("meta"):
            try:
                os.remove(g["meta"])
            except OSError:
                pass
        stats["files"] += len(moved)
        stats["runs"] += 1
        if not moved.get("out"):
            stats["leftover"] += 1          # 只有输入、没有结果的半途任务，也归档但不进任务表

        # 有结果就补一份 job.json：老结果也能靠 ?job=<id> 打开
        if moved.get("out"):
            j = dict(meta)
            # 旧记录没存过原始文件名时，别拿 "input.png" 冒充 —— 老实说不知道
            j.update({
                "id": jid, "state": "done", "dir": d, "queued": t,
                "name": meta.get("name") or ("未记录文件名" + (ext or ".png")),
                "src": moved.get("in"), "out": moved.get("out"),
                "baseline": moved.get("bicubic"),
                "view": moved.get("view"), "view_in": moved.get("view_in"),
                "tiles": {},
            })
            for which in ("input", "result"):
                td = os.path.join(d, "tiles", which)
                if os.path.isdir(td):
                    j["tiles"][which] = td
            if not meta:
                # 老记录没留日志：能推出来的补齐，推不出来的留空由界面显示「—」，
                # 不编数字 —— 宁可空着也别报一个看不出来的假值。
                j["recovered"] = True
                for key, kk in (("out", ("out_w", "out_h")), ("src", ("in_w", "in_h"))):
                    p = j.get(key)
                    if p and os.path.isfile(p):
                        try:
                            with Image.open(p) as im:
                                j[kk[0]], j[kk[1]] = im.size
                        except Exception:
                            pass
                if j.get("out_w") and j.get("in_w"):
                    j["scale"] = max(1, int(round(j["out_w"] / j["in_w"])))
            save_meta(adopt(j))
    return stats


# ------------------------------------------------------------ 模型自检 -- #
def model_report() -> dict:
    """逐个点数：每个预设要用的 onnx（含只降噪那一份）到底在不在。"""
    missing: list[str] = []
    total = have = 0
    for v in U.PRESETS.values():
        files = set(v["noise"].values())
        if v.get("dn_only"):
            files.add(v["dn_only"])
        for f in sorted(files):
            total += 1
            p = os.path.join(U.MODEL_DIR, f)
            if os.path.isfile(p) and os.path.getsize(p) > 1024:
                have += 1
            else:
                missing.append(f)
    return {"have": have, "total": total, "ok": not missing,
            "missing": missing, "dir": U.MODEL_DIR,
            "hint": "python tools/download_models.py"}


def print_model_report(rep: dict) -> None:
    if rep["ok"]:
        print(U.t("srv.model.ok", have=rep["have"], total=rep["total"], dir=rep["dir"]))
    else:
        print(U.t("srv.model.bad", n=len(rep["missing"]), total=rep["total"]))
        for f in rep["missing"]:
            print(f"    ✗ {f}")
        print(U.t("srv.model.dl", hint=rep["hint"]))
    probe = os.path.join(U.MODEL_DIR, ".device_probe.json")
    print(U.t("srv.probe.cached") if os.path.isfile(probe) else U.t("srv.probe.first"))


# ------------------------------------------------------------------ 工具 -- #
def safe_file(base: str, rel: str, allowed_ext: set[str]) -> str | None:
    """把 /fonts/xxx 这类相对路径钉在 base 里面，挡掉 ../ 穿越。"""
    name = urllib.parse.unquote(rel).lstrip("/\\")
    if not name or os.path.isabs(name):
        return None
    p = os.path.normpath(os.path.join(base, name))
    if not p.startswith(os.path.normpath(base) + os.sep):
        return None
    if os.path.splitext(p)[1].lower() not in allowed_ext:
        return None
    return p if os.path.isfile(p) else None


def center_crop(a: np.ndarray, size: int) -> np.ndarray:
    h, w = a.shape[:2]
    ch, cw = min(size, h), min(size, w)
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    return a[y0:y0 + ch, x0:x0 + cw]


def png_bytes(im: Image.Image, level: int = 4) -> bytes:
    buf = BytesIO()
    im.save(buf, "PNG", compress_level=level)
    return buf.getvalue()


def u8(rgb: np.ndarray) -> Image.Image:
    return Image.fromarray((rgb * 255 + 0.5).astype(np.uint8))


def baseline_patch(src_rgb: np.ndarray, out_wh: tuple[int, int],
                   box: tuple[int, int, int, int]) -> np.ndarray | None:
    """
    只把 box（输出坐标）那一小块的双三次插值算出来。

    以前是整幅插值：6744×10112 转 float32 要 816 MB，而且算完只取中心 512×512 ——
    几十秒的内存抖动换一个 0.5 MP 的方差。现在把源图对应区域先裁出来再放大，
    补一圈 padding 让边缘也跟整幅插值对得上。
    """
    ow, oh = out_wh
    ih, iw = src_rgb.shape[:2]
    sx, sy = ow / iw, oh / ih
    x0, y0, x1, y1 = box
    X0 = max(0, int(math.floor((x0 - TILE_PAD) / sx)))
    Y0 = max(0, int(math.floor((y0 - TILE_PAD) / sy)))
    X1 = min(iw, int(math.ceil((x1 + TILE_PAD) / sx)))
    Y1 = min(ih, int(math.ceil((y1 + TILE_PAD) / sy)))
    if X1 <= X0 or Y1 <= Y0:
        return None

    sub = src_rgb[Y0:Y1, X0:X1]
    tw = max(1, int(round((X1 - X0) * sx)))
    th = max(1, int(round((Y1 - Y0) * sy)))
    up = np.asarray(u8(sub).resize((tw, th), Image.BICUBIC), np.float32) / 255.0

    ox = int(round(x0 - X0 * sx))
    oy = int(round(y0 - Y0 * sy))
    patch = up[max(0, oy):oy + (y1 - y0), max(0, ox):ox + (x1 - x0)]
    if patch.shape[0] < (y1 - y0) or patch.shape[1] < (x1 - x0):
        return None         # 边缘兜底：宁可少一项指标，也不报一个错的数
    return patch


def measure(src_rgb: np.ndarray, out8: np.ndarray, scale: int, t_net: float) -> dict:
    """
    算指标。刻意都在受控尺寸上做，跟输出多大无关：
      · 回环一致 —— 缩回原尺寸，规模等于输入，必然便宜
      · 锐度对比 —— 只看中心 512×512 那一块的双三次基线
    out8 传 uint8，别让这里再持有一份 816 MB 的 float32。
    """
    h0, w0 = src_rgb.shape[:2]
    h1, w1 = out8.shape[:2]
    res: dict = {"mp": mp_of(w1, h1), "scale_actual": round(w1 / w0, 3)}

    back = np.asarray(Image.fromarray(out8).resize((w0, h0), Image.LANCZOS),
                      np.float32) / 255.0
    res["rt_psnr"] = round(M.psnr(back, src_rgb), 2)
    res["rt_ssim"] = round(M.ssim(back, src_rgb), 4)

    cw, ch = min(METRIC_CROP, w1), min(METRIC_CROP, h1)
    x0, y0 = (w1 - cw) // 2, (h1 - ch) // 2
    box = (x0, y0, x0 + cw, y0 + ch)
    oc = np.asarray(Image.fromarray(out8).crop(box), np.float32) / 255.0
    bc = baseline_patch(src_rgb, (w1, h1), box)
    if bc is not None:
        s_out, s_bic = M.sharpness(oc), M.sharpness(bc)
        res["sharp_out"] = round(s_out, 5)
        res["sharp_bicubic"] = round(s_bic, 5)
        res["sharp_gain"] = round(s_out / s_bic, 2) if s_bic > 1e-9 else None

    if t_net > 0:
        res["mps"] = round(res["mp"] / t_net, 2)
    return res


# --------------------------------------------------------------- 缩略预览 -- #
def preview_path(src: str, dst: str, max_side: int) -> str:
    """
    生成（或复用）一张缩略预览。本来就够小就直接把原文件当预览，不折腾。
    光台 / 滑杆只碰这张，浏览器永远不用解码 68 MP。

    注意这只影响「看」，跟下载的量无关 —— 下载接口 不带 ?view ，
    给出去的始终是模型原生分辨率的无损 PNG。
    """
    with Image.open(src) as im:
        w, h = im.size
        if max(w, h) <= max_side:
            return src
        if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
        scale = max_side / max(w, h)
        tw, th = max(1, round(w * scale)), max(1, round(h * scale))
        im.load()                         # 先完整解码，再缩 —— convert(mode) 会多复制一整份
        im.resize((tw, th), Image.LANCZOS).save(dst, "PNG", compress_level=4)
    return dst


def view_of(j: dict, which: str) -> str | None:
    """
    缩略预览取哪个文件：有就拿来用，没有就现生成一份丢进工程文件夹。

    老记录（整理时捡回来的）当年没生成过预览，光台要是直接吃 23 MP 的原图，
    就又把「大图预览卡」那个坑踩回去了。现生成一次，之后都走缓存。
    """
    src = j.get("out") if which == "result" else j.get("src")
    if not src or not os.path.isfile(src):
        return None
    key = "view" if which == "result" else "view_in"
    if j.get(key) and os.path.isfile(j[key]):
        return j[key]
    dst = os.path.join(j["dir"], f"preview-{which}.png")
    try:
        got = preview_path(src, dst, PREVIEW_MAX)
    except OSError:
        return src
    j[key] = None if got == src else got      # 本来就够小，不必留个一模一样的副本
    return got


# --------------------------------------------------------------- 瓦片 -- #
def tile_dir(d: str, which: str) -> str:
    return os.path.join(d, "tiles", which)


def build_tiles(d: str, which: str, path: str) -> tuple[str, int, int] | None:
    """
    把一张大图切成 1024² 的 PNG 瓦片。

    放大镜要的是「一小块真像素」，而 PNG 没法随机访问 —— 每取一次块就解码整幅 68 MP，
    鼠标一动就卡死。切好之后取块只读命中的那 1-4 张，几十毫秒。
    瓦片是纯无损的像素切片，切与不切，下载到的结果一个字节都不变。
    """
    tdir = tile_dir(d, which)
    lock = TILE_LOCKS.setdefault(tdir, threading.Lock())
    with lock:
        if os.path.isdir(tdir):
            for fn in os.listdir(tdir):
                if fn == ".ready":
                    with open(os.path.join(tdir, fn), encoding="utf-8") as f:
                        rows, cols = (int(x) for x in f.read().split())
                    return tdir, rows, cols
        os.makedirs(tdir, exist_ok=True)
        im = Image.open(path)
        im.load()                          # 一次性全解码，后面 crop 才是内存拷贝
        W, H = im.size
        rows = (H + TILE_PX - 1) // TILE_PX
        cols = (W + TILE_PX - 1) // TILE_PX
        for r in range(rows):
            for c in range(cols):
                box = (c * TILE_PX, r * TILE_PX,
                       min(W, (c + 1) * TILE_PX), min(H, (r + 1) * TILE_PX))
                im.crop(box).save(os.path.join(tdir, f"{r}_{c}.png"), "PNG", compress_level=3)
        im.close()
        with open(os.path.join(tdir, ".ready"), "w", encoding="utf-8") as f:
            f.write(f"{rows} {cols}")
        return tdir, rows, cols


def view_region(j: dict, x: int, y: int, w: int, h: int,
                dw: int, dh: int) -> Image.Image | None:
    """从缩略预览里取同一块，缩到屏幕尺寸。

    放大镜最远那几档要的是一大块画面压进一格：那块真像素得从瓦片里拼
    （几千万像素、几十 MB 内存、半秒起），而缩略预览本来就是整幅的一次性缩放，
    这一档从它取，几十毫秒就回来了，压到一格大小后屏幕上根本看不出差别。

    唯一要判的是「预览缩完还够不够画这一格」—— 不够就返回 None，让调用方走原像素。
    1:1 那一档不会走到这儿（dw == 边长，压根不缩），拿到的还是模型的原始像素。
    """
    p = j.get("view")
    if not p or not os.path.isfile(p):
        return None
    W = j.get("out_w") or 0
    if not W:
        return None
    try:
        with Image.open(p) as src:
            src.load()
            pw, ph = src.size
            if pw >= W:                     # 预览没缩过，跟原图一样，没必要绕这一道
                return None
            k = pw / W
            if w * k < dw * 0.85:           # 撑不起这一格，会糊
                return None
            box = (int(x * k), int(y * k),
                   max(int(x * k) + 1, int(round((x + w) * k))),
                   max(int(y * k) + 1, int(round((y + h) * k))))
            return src.crop(box).resize((dw, dh), Image.LANCZOS)
    except OSError:
        return None


def read_region(j: dict, which: str, x: int, y: int, w: int, h: int) -> Image.Image | None:
    """从 result / input 里取一块。有瓦片就走瓦片，没有就现解码（小图才走这条路）。"""
    nat = {"result": (j.get("out_w"), j.get("out_h"), j.get("out")),
           "input": (j.get("in_w"), j.get("in_h"), j.get("src"))}[which]
    W, H, path = nat
    if not path or not os.path.isfile(path) or not W or not H:
        return None
    x = int(clamp(x, 0, max(0, W - 1)))
    y = int(clamp(y, 0, max(0, H - 1)))
    w = int(clamp(w, 1, W - x))
    h = int(clamp(h, 1, H - y))

    d = (j.get("tiles") or {}).get(which)
    if d and os.path.isdir(d):
        rows = (H + TILE_PX - 1) // TILE_PX
        cols = (W + TILE_PX - 1) // TILE_PX
        r0, r1 = y // TILE_PX, (y + h - 1) // TILE_PX
        c0, c1 = x // TILE_PX, (x + w - 1) // TILE_PX
        if r1 >= rows or c1 >= cols:
            return None
        with Image.open(os.path.join(d, "0_0.png")) as probe:
            mode = probe.mode
        acc = Image.new(mode, (w, h))
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                with Image.open(os.path.join(d, f"{r}_{c}.png")) as t:
                    t.load()
                    px = c * TILE_PX
                    py = r * TILE_PX
                    acc.paste(t, (px - x, py - y))
        return acc

    with Image.open(path) as im:
        im.load()
        return im.crop((x, y, x + w, y + h))


# ------------------------------------------------------------------ 任务 -- #
def run_job(jid: str) -> None:
    j = JOBS[jid]
    j["state"] = "running"
    j["started"] = now()
    d = j["dir"]
    try:
        rgb, alpha, mode = U.load_image(j["src"])
        h0, w0 = rgb.shape[:2]
        j["in_w"], j["in_h"] = w0, h0
        j["in_mp"] = mp_of(w0, h0)
        j["stage"] = U.t("st.model")
        runner = U.build_runner(j["preset"], j["scale"], j["denoise"],
                                tile=j["tile"], overlap=OVERLAP)
        passes, net = U.plan_passes(j["scale"], runner.scale)
        j["model"] = runner.name
        j["device"] = runner.device.upper()
        j["passes"] = passes
        j["net_scale"] = net
        j["stage"] = U.t("st.infer")

        # 进网络之前先把源图的 JPEG 振铃压掉：细节模型会把那几个灰阶的振铃当成
        # 纹理，放大成一片网纹（眼睛、嘴这类「小尺寸 + 四周全是强边」的地方最明显）。
        # 这一步必须和 app/core/pipeline.py 的 upscale() 保持一致 —— 命令行走那条路，
        # 网页走这条，两边接的东西不一样，同一张图就会出两种结果，而界面和指标
        # 都看不出来。app/qa/smoke.py 的 4d 用「网页结果 == app.core.upscale」锁住它。
        cur = U.declip(rgb)
        t0 = now()
        for p in range(passes):
            def cb(done, total, el, _p=p):
                j["stage"] = (U.t("st.pass", n=_p + 1, total=passes) if passes > 1
                              else U.t("st.net"))
                j["progress"] = round(100.0 * (_p + done / total) / passes)
                j["elapsed"] = now() - j["started"]

            cur = runner.upscale(cur, progress=cb)
        t_net = now() - t0

        if net != j["scale"]:       # 网络倍率跟目标对不上时，最后用 Lanczos 凑齐
            th, tw = h0 * j["scale"], w0 * j["scale"]
            cur = np.asarray(u8(cur).resize((tw, th), Image.LANCZOS),
                             np.float32) / 255.0

        # 收尾锐化：网络出来的边是渐变过渡，一条 1px 细线摊到 4x 就是一条灰带。
        # 这一步只加在边缘上（平坦处 rgb≈模糊版，加的是零），所以不会磨出噪点。
        sh_r, sh_g = U.resolve_sharpen(j["preset"], j.get("clear", "normal"))
        if sh_g > 0:
            j["stage"] = U.t("st.sharpen", name=U.label("clear", j.get("clear", "normal")))
            cur = U.unsharp(cur, sh_r, sh_g)
        # 注意：这里存的是「引擎参数」，字段名别跟指标 res["sharp_gain"]（锐度倍率，
        # 后面 j.update(measure(...)) 会写进来）撞车 —— 撞了的话界面会把 49.4 这种
        # 倍率当成锐化增益显示出来。
        j["sharp_radius"], j["sharp_amount"] = sh_r, round(sh_g, 3)

        # 明暗开关放在最后：纯观感调整，不该影响上面任何一步的判断。
        # 落盘的字段名是 bright_factor，跟指标里的任何字段都不撞。
        k_b = U.resolve_bright(j.get("bright", "off"))
        if abs(k_b - 1.0) > 1e-9:
            j["stage"] = U.t("st.bright", name=U.label("bright", j.get("bright", "off")))
            cur = U.brighten(cur, k_b)
        j["bright_factor"] = round(k_b, 4)

        j["stage"] = U.t("st.save")
        j["progress"] = 100
        out = os.path.join(d, "result.png")
        U.save_image(cur, out, U.upscale_alpha(alpha, j["scale"]))
        j["out"] = out
        j["out_w"], j["out_h"] = cur.shape[1], cur.shape[0]
        j["out_bytes"] = os.path.getsize(out)
        j["in_bytes"] = os.path.getsize(j["src"])

        out8 = (cur * 255 + 0.5).astype(np.uint8)
        del cur                     # 68 MP 的 float32 是 816 MB，指标阶段不再需要它

        j["stage"] = U.t("st.measure")
        j.update(measure(rgb, out8, j["scale"], t_net))

        # 双三次基线：体积对照和放大镜中格的兜底。整幅落盘，所以只在输得起的尺寸上做。
        if j["mp"] <= MAX_BASELINE_MP:
            base = u8(rgb).resize((j["out_w"], j["out_h"]), Image.BICUBIC)
            data = png_bytes(base)
            bp = os.path.join(d, "bicubic.png")
            with open(bp, "wb") as f:
                f.write(data)
            j["baseline"] = bp
            j["bicubic_bytes"] = len(data)
            j["has_baseline"] = True
        else:
            j["has_baseline"] = False      # 输出太大没算，界面上会说明原因
        del out8

        j["stage"] = U.t("st.preview")
        j["view"] = preview_path(j["out"], os.path.join(d, "preview-result.png"), PREVIEW_MAX)
        j["view_in"] = preview_path(j["src"], os.path.join(d, "preview-input.png"), PREVIEW_MAX)

        j["stage"] = U.t("st.tiles")
        tiles: dict[str, str] = {}
        for which, p, mpx in (("result", j["out"], j["mp"]),
                              ("input", j["src"], j["in_mp"])):
            if p and os.path.isfile(p) and mpx > TILE_IF_MP:
                got = build_tiles(d, which, p)
                if got:
                    tiles[which] = got[0]
        j["tiles"] = tiles
        j["tiled"] = sorted(tiles)      # 给界面和自检看：哪几层预切过块

        j["t_net"] = round(t_net, 2)
        j["elapsed"] = now() - j["started"]
        j["state"] = "done"
    except Exception as e:
        j["state"] = "error"
        j["msg"] = f"{type(e).__name__}: {e}"
        # 界面上只显示一行 msg，光看那句没法定位。traceback 落到服务端终端，
        # 排查时不用再猜「这行错误是哪来的」。
        import traceback
        traceback.print_exc()
    finally:
        j["progress"] = 100 if j["state"] == "done" else j.get("progress", 0)
        save_meta(j)


def worker() -> None:
    while True:
        jid = TASK_Q.get()
        try:
            run_job(jid)
        finally:
            TASK_Q.task_done()


def evict() -> None:
    """暂存模式的淘汰：先按条数、再按磁盘预算，从最早完成的开始。

    保存模式（默认）什么都不删 —— 结果既然说了要留，就一条都不动，
    磁盘占用摆在界面上，要不要清由人决定，不替他做主。
    """
    if CFG["keep"]:
        return
    with JOBS_LOCK:
        done = sorted((x for x in JOBS.values() if x["state"] in ("done", "error")),
                      key=lambda x: x.get("queued") or 0)
        drop = len(JOBS) - MAX_JOBS_TEMP
        total = sum(dir_size(x.get("dir") or "") for x in JOBS.values())
        i = 0
        while (drop > 0 or total > DISK_TEMP) and i < len(done):
            x = done[i]
            i += 1
            total -= dir_size(x.get("dir") or "")
            drop -= 1
            JOBS.pop(x["id"], None)
            drop_job(x)


# --------------------------------------------------------------- 请求处理 -- #
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "BigPixels/1.3"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # 放大镜每动一下就打三个 /detail，一条都别往控制台写 —— 刷屏把启动信息都冲没了。
        # 注意 requestline 在 args[0] 里，不在 fmt 里，一开始就写错在这儿。
        line = str(args[0]) if args else ""
        if "/api/job/" in line and line.startswith("GET"):
            return
        sys.stderr.write("  %s\n" % (fmt % args))

    # ---- 工具 ----
    def _send(self, code: int, body: bytes,
              ctype="application/json; charset=utf-8",
              cache="no-store", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _file(self, path: str, cache="no-store", dl_name: str | None = None):
        ext = os.path.splitext(path)[1].lower()
        ctype = CT.get(ext)
        if ctype is None:
            ctype = {"woff2": "font/woff2", "json": "application/json"}.get(
                ext.lstrip("."), "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        extra = {"Content-Disposition": f'inline; filename="{dl_name or os.path.basename(path)}"'}
        self._send(200, body, ctype, cache, extra)

    # ---- GET ----
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)

        if path in ("/", "/index.html"):
            p = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(p):
                return self._err(500, U.t("api.no_index"))
            with open(p, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")

        if path == "/js/i18n.js":
            return self._i18n_js()

        for prefix, base in (("/fonts/", FONT_DIR), ("/demo/", DEMO_DIR),
                             ("/css/", CSS_DIR), ("/js/", JS_DIR)):
            if path.startswith(prefix):
                p = safe_file(base, path[len(prefix):], STATIC_OK[prefix.strip("/")])
                if not p:
                    return self._err(404, U.t("api.no_file"))
                if prefix == "/fonts/":
                    # 字体不会改，缓存一年
                    cache = "public, max-age=31536000, immutable"
                elif prefix in ("/css/", "/js/"):
                    # 版式和脚本随手就改，缓存住的话看到的还是上一版 —— 本地回环，每次重取最省心
                    cache = "no-store"
                else:
                    cache = "public, max-age=3600"
                return self._file(p, cache)

        if path == "/api/presets":
            return self._json(self._presets())

        if path == "/api/settings":
            return self._json(CFG)

        if path.startswith("/api/job/"):
            return self._job(path, q)

        return self._err(404, "not found")

    def do_HEAD(self):
        self.do_GET()

    def _i18n_js(self):
        """界面上的静态文案：服务端按当前语言现拼一份 JS，页面同步读它。

        为什么不走 /api/presets 一起发：页面骨架里本来就写着中文（词的兜底），
        要是等接口回来再换，英文/日文用户会先看见一帧中文再跳成自己的语言。
        现拼一份同步脚本，页面一遍渲染出来就是对的；顺带把 <html lang> 也定下来。
        """
        lang = U.lang_of()
        payload = {
            "lang": lang,
            "locale": {"zh": "zh-CN", "en": "en", "ja": "ja"}.get(lang, "zh-CN"),
            "names": U.LANG_NAMES,
            "order": list(U.LANGS),
            "mb": MAX_UPLOAD // 1048576,
            "ui": U.web_table(),
        }
        body = ("/* 由 app/server/server.py 按当前界面语言现拼，别手改 —— "
                "要改词去 app/locales/<lang>.json */\n"
                "window.I18N = " + json.dumps(payload, ensure_ascii=False) + ";\n"
                "document.documentElement.lang = " + json.dumps(payload["locale"]) + ";\n")
        return self._send(200, body.encode("utf-8"), "text/javascript; charset=utf-8")

    def _presets(self):
        presets = []
        for k, v in U.PRESETS.items():
            files = set(v["noise"].values())
            if v.get("dn_only"):
                files.add(v["dn_only"])
            have = sum(1 for f in files if os.path.isfile(os.path.join(U.MODEL_DIR, f)))
            presets.append({"id": k, "label": U.label("preset", k),
                            "desc": U.t(f"preset.{k}.desc"),
                            "tech": U.t(f"preset.{k}.tech"), "scale": v.get("hint", 2),
                            "ready": have == len(files),
                            "have": have, "total": len(files)})
        return {
            "presets": presets,
            # 档位名与说明一律按当前语言现取 —— 界面上不再写死任何一种语言
            "denoise": [{"id": i, "label": U.label("denoise", i)}
                        for i in ("none", "low", "medium", "high", "highest")],
            "clear": [{"id": i, "label": U.label("clear", i),
                       "desc": U.t(f"clear.{i}.desc")} for i in ("soft", "normal", "crisp")],
            "bright": [{"id": i, "label": U.label("bright", i),
                        "desc": U.t(f"bright.{i}.desc")} for i in ("off", "lift")],
            "lang": U.lang_of(),
            "langs": [{"id": c, "name": U.LANG_NAMES[c]} for c in U.LANGS],
            "ui": U.web_table(),
            "scales": [2, 4, 8, 16],
            "tiles": [128, 192, 256, 384, 512],
            "providers": U.ort.get_available_providers(),
            "cpus": os.cpu_count(),
            "overlap": OVERLAP,
            "samples": self._samples(),
            "limits": {"max_upload": MAX_UPLOAD,
                       "max_baseline_mp": MAX_BASELINE_MP,
                       "preview_max": PREVIEW_MAX,
                       "tile_req_max": TILE_REQ_MAX,
                       "disk_temp": DISK_TEMP},
            "models_dir": U.MODEL_DIR,
            "work_dir": WORK_DIR,
            "work_rel": os.path.relpath(WORK_DIR, ROOT).replace("\\", "/") + "/",
            "models": model_report(),
            "settings": dict(CFG),
            "disk": {"used": dir_size(WORK_DIR), "jobs": len(JOBS)},
            "device_probe": self._probe(),
        }

    def _samples(self):
        """首页示例图：文件名到说明的映射写死在这里，界面上当「示例」明示。

        三张示例的固定文件名（lineart / text / grain）与词条键一一对应；
        目录里多出来的别的图不编说明，只报文件名。
        """
        known = ("lineart", "text", "grain")
        out = []
        if not os.path.isdir(DEMO_DIR):
            return out
        for fn in sorted(os.listdir(DEMO_DIR)):
            if os.path.splitext(fn)[1].lower() not in STATIC_OK["demo"]:
                continue
            stem = os.path.splitext(fn)[0]
            label, note = ((U.t(f"sample.{stem}.name"), U.t(f"sample.{stem}.note"))
                           if stem in known else (stem, ""))
            try:
                with Image.open(os.path.join(DEMO_DIR, fn)) as im:
                    w, h = im.size
            except Exception:
                continue
            out.append({"id": stem, "url": f"/demo/{fn}", "name": label,
                        "note": note, "w": w, "h": h})
        out.sort(key=lambda s: (list(known).index(s["id"]) if s["id"] in known else 9))
        return out

    def _probe(self):
        p = os.path.join(U.MODEL_DIR, ".device_probe.json")
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _job(self, path: str, q: dict):
        parts = [p for p in path.split("/") if p]
        if len(parts) < 3:
            return self._err(404, "bad path")
        jid, what = parts[2], (parts[3] if len(parts) > 3 else None)
        with JOBS_LOCK:
            j = JOBS.get(jid)
        if not j:
            return self._err(404, U.t("api.no_job"))
        j["keep"] = bool(CFG["keep"])

        want_view = (q.get("view") or ["0"])[0] in ("1", "true", "yes")

        if what == "detail":
            if j["state"] != "done":
                return self._err(409, U.t("api.busy"))
            return self._detail(j, q)

        if what in ("result", "input", "baseline"):
            if j["state"] != "done":
                return self._err(409, U.t("api.busy"))
            key = {"result": "out", "input": "src", "baseline": "baseline"}[what]
            if want_view and what in ("result", "input"):
                p = view_of(j, what) or j.get(key)
            else:
                p = j.get(key)
            if not p or not os.path.isfile(p):
                return self._err(404, U.t("api.no_artifact"))
            return self._file(p, "private, max-age=3600",
                              f"{jid}_{'ai' if what == 'result' else what}"
                              f"{os.path.splitext(p)[1]}")

        return self._json(job_json(j))

    def _detail(self, j: dict, q: dict):
        """
        放大镜要的那一小块。坐标统一按「输出像素」说，服务端自己换算回原图 ——
        这样前端不用关心 scale_actual 是多少，也不用自己做除法。

        dw 是「这一块最终在屏幕上画多宽」。倍率比 1:1 更远的时候，前端要的是
        一大块画面压进一格 —— 与其把整块原像素发过去让浏览器缩，不如在这儿缩好，
        传输和浏览器解码都掉一个数量级。dw >= 原始边长就一个字都不动，
        1:1 那一档拿到的还是模型的原始像素。
        """
        g = lambda k, d="0": (q.get(k) or [d])[0]      # noqa: E731
        layer = g("layer", "result")
        if layer not in ("input", "bicubic", "result"):
            return self._err(400, U.t("api.bad_layer"))
        try:
            x, y, w, h = (int(g("x")), int(g("y")), int(g("w")), int(g("h")))
        except ValueError:
            return self._err(400, U.t("api.bad_int"))
        if w <= 0 or h <= 0:
            return self._err(400, U.t("api.bad_wh"))
        if w > TILE_REQ_MAX or h > TILE_REQ_MAX or w * h > TILE_REQ_MP * 1e6:
            return self._err(400, U.t("api.too_big", side=TILE_REQ_MAX, mp=TILE_REQ_MP))
        try:
            dw = int(float(g("dw", "0") or 0))
        except ValueError:
            return self._err(400, U.t("api.bad_dw"))
        if dw < 0 or dw > TILE_REQ_MAX:
            return self._err(400, U.t("api.bad_dw_range", max=TILE_REQ_MAX))

        W, H = j["out_w"], j["out_h"]
        x = int(clamp(x, 0, max(0, W - 1)))
        y = int(clamp(y, 0, max(0, H - 1)))
        w = int(clamp(w, 1, W - x))
        h = int(clamp(h, 1, H - y))
        dh = max(1, int(round(dw * h / w))) if dw else 0

        if layer == "result":
            im = None
            if dw and dw < w:
                im = view_region(j, x, y, w, h, dw, dh)   # 看得远那几档：走缩略预览，快十倍
            if im is None:
                im = read_region(j, "result", x, y, w, h)
            if im is None:
                return self._err(404, U.t("api.bad_result"))
            if dw and dw < im.width:
                im = im.resize((dw, dh), Image.LANCZOS)
        else:
            sr = W / j["in_w"]
            ix, iy = int(math.floor(x / sr)), int(math.floor(y / sr))
            iw = max(1, int(math.ceil(w / sr)))
            ih = max(1, int(math.ceil(h / sr)))
            sub = read_region(j, "input", ix, iy, iw, ih)
            if sub is None:
                return self._err(404, U.t("api.bad_input"))
            if layer == "input":
                # 原图那一格要的是它自己的像素，交给前端用最近邻放大 —— 别在这儿插值
                im = sub
            else:
                # 双三次基线直接算到显示尺寸：先插到输出尺寸再缩回来是白烧 CPU，
                # 而且两级重采样比一次性插值更不像「双三次」。
                im = sub.resize((dw, dh) if dw else (w, h), Image.BICUBIC)

        # 这一块是给鼠标追着用的，编码档位压到最低 —— 走本机回环，字节数不值钱
        return self._send(200, png_bytes(im, level=1), "image/png", "private, max-age=3600")

    # ---- DELETE ----
    def do_DELETE(self):
        """删掉一次工程：记录和文件夹一起走。

        主要给测试脚本用 —— smoke 每跑一次就落一个真工程在 outputs/web 里，
        不收拾的话界面上会越堆越多。任务还在跑就先别删，免得推理写到一半目录没了。
        """
        u = urllib.parse.urlparse(self.path)
        if not u.path.startswith("/api/job/"):
            return self._err(404, "not found")
        jid = u.path[len("/api/job/"):]
        with JOBS_LOCK:
            j = JOBS.get(jid)
            if not j:
                return self._err(404, U.t("api.no_job2"))
            if j.get("state") in ("queued", "running"):
                return self._err(409, U.t("api.job_running"))
            JOBS.pop(jid, None)
        drop_job(j)
        return self._json({"ok": True, "id": jid})

    # ---- POST ----
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        if u.path == "/api/settings":
            raw_keep = (q.get("keep") or [None])[0]
            raw_lang = (q.get("lang") or [None])[0]
            if raw_keep is None and raw_lang is None:
                return self._err(400, U.t("api.bad_keep"))
            if raw_lang is not None:
                if not U.i18n.valid(raw_lang):
                    return self._err(400, U.t("api.bad_lang"))
                U.i18n.set_lang(raw_lang)      # 落盘 + 换掉进程内的当前语言
                CFG.update(U.settings.load())
            if raw_keep is not None:
                CFG["keep"] = raw_keep in ("1", "true", "yes", "on")
                save_settings()
                # 切回保存模式时清一次（evict 自己会在保存模式下直接返回）——
                # 切过去暂存不清：界面上说的是「服务停止即清空」，不是立刻清。
                if CFG["keep"]:
                    evict()
            return self._json(CFG)

        if u.path != "/api/job":
            return self._err(404, "not found")

        preset = (q.get("preset") or ["anime"])[0]
        denoise = (q.get("denoise") or ["medium"])[0]
        clear = (q.get("clear") or ["normal"])[0]
        bright = (q.get("bright") or ["off"])[0]
        name = (q.get("name") or ["upload.png"])[0]
        try:
            scale = int((q.get("scale") or ["4"])[0])
            tile = int((q.get("tile") or ["256"])[0])
        except ValueError:
            return self._err(400, U.t("api.bad_scale"))
        if preset not in U.PRESETS:
            return self._err(400, U.t("api.bad_preset", name=preset))
        if clear not in U.CLEAR_LEVELS:
            return self._err(400, U.t("api.bad_clear", name=clear))
        if bright not in U.BRIGHT_LEVELS:
            return self._err(400, U.t("api.bad_bright", name=bright))
        if scale not in (1, 2, 4, 8, 16):
            return self._err(400, U.t("api.bad_scale_val"))
        if tile not in (128, 192, 256, 384, 512):
            return self._err(400, U.t("api.bad_tile"))

        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._err(400, U.t("api.no_data"))
        if n > MAX_UPLOAD:
            return self._err(413, U.t("api.too_large", mb=MAX_UPLOAD // 1048576))
        data = self.rfile.read(n)

        jid = uuid.uuid4().hex[:12]
        ext = os.path.splitext(name)[1].lower()
        if ext not in CT:
            ext = ".png"
        t = now()
        d = os.path.join(WORK_DIR, job_dir_name(jid, t, name))
        try:
            os.makedirs(d, exist_ok=True)
            src = os.path.join(d, "input" + ext)
            with open(src, "wb") as f:
                f.write(data)
            with Image.open(src) as im:     # 先解码一遍，脏数据不进队列
                im.verify()
        except Exception as e:
            if os.path.isdir(d) and JOB_DIR_RE.match(os.path.basename(d)):
                rm_tree(d)
            return self._err(400, U.t("api.decode_fail", err=e))

        j = {"id": jid, "state": "queued", "progress": 0, "stage": U.t("st.queue"),
             "preset": preset, "preset_label": U.label("preset", preset),
             "scale": scale, "denoise": denoise, "clear": clear,
             "bright": bright, "tile": tile,
             "name": name, "src": src, "queued": t, "elapsed": 0.0,
             "dir": d, "rel": os.path.relpath(d, ROOT), "keep": bool(CFG["keep"]),
             "tiles": {}}
        with JOBS_LOCK:
            JOBS[jid] = j
        TASK_Q.put(jid)
        evict()
        return self._json({"id": jid})


# --------------------------------------------------------------------------- #
# 对外报哪个地址 —— 别把 bind 地址当目的地址用
# --------------------------------------------------------------------------- #
# 这是个真踩过的坑：双击启动那条路，服务明明起得好好的，浏览器却说"无法访问此页面"，
# 窗口里那一行写着 http://0.0.0.0:8765。
#
# 原因是 0.0.0.0 是**通配地址**，它的意思是「所有网卡都收」——那是给 bind() 看的。
# 它本身不是一个能连的目的地址，浏览器拿它当目标就直接拒（Windows/Chrome 上
# 报 ERR_ADDRESS_INVALID）。所以：**绑在哪儿**和**从哪儿能打开**要分开算。
WILDCARD_HOSTS = ("", "*", "0.0.0.0", "::", "[::]", "0:0:0:0:0:0:0:0")

# 这几个网段的地址**看着像内网、其实别的设备连不上**，不能当成"局域网地址"报出去：
#   198.18.0.0/15 —— RFC 2544 的性能测试网段，代理软件的 TUN 网卡爱用
#   127.0.0.0/8 —— 回环  169.254.0.0/16 —— 没拿到 DHCP 时的自分配地址
# 实测这台机器上代理的 TUN 就占了 198.18.0.1，而且**默认路由在它手上** ——
# 于是「connect 一个外网地址看内核挑了哪张网卡」这个常用探针每次都返回它。
# 所以不能只信探针：要枚举所有网卡，再按可连性排个优先级。
_IGNORE_NETS = ("127.0.0.0/8", "169.254.0.0/16", "198.18.0.0/15")
# 越靠前越像"真·局域网"：家里的 192.168、公司的 10.x，172.16/12 是 Docker 之类的常客
_PREFER_NETS = ("192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12")


def _probe_ip() -> str | None:
    """内核到外网会走哪张网卡。不发任何包（UDP connect 只是选路），但可能被 TUN 带偏。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _ipv4_candidates() -> list[str]:
    """本机能拿出来的局域网 IPv4，最像"真局域网"的排前面。

    枚举（hostname 解析）+ 探针两条路一起用：枚举能拿到所有网卡，探针在 DNS 不好使时兜底。
    两边都会被虚拟网卡污染，所以统一过一遍筛子。
    """
    import ipaddress
    cands: list[str] = []
    try:
        cands += [ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None,
                                                         socket.AF_INET)]
    except OSError:
        pass
    p = _probe_ip()
    if p:
        cands.append(p)

    ignore = [ipaddress.ip_network(n) for n in _IGNORE_NETS]
    keep: list[str] = []
    for c in cands:
        try:
            a = ipaddress.ip_address(c)
        except ValueError:
            continue
        if a.is_loopback or a.is_link_local or any(a in n for n in ignore):
            continue
        if c not in keep:
            keep.append(c)

    prefer = [ipaddress.ip_network(n) for n in _PREFER_NETS]

    def rank(c: str) -> int:
        a = ipaddress.ip_address(c)
        for i, n in enumerate(prefer):
            if a in n:
                return i
        return len(prefer)

    return sorted(keep, key=rank)


def _local_ipv4s() -> list[str]:
    """给启动横幅用：探不到就返回空列表。

    外面包一层是刻意的 —— 这只是**顺手指个路**，不该有本事让服务起不来。
    上面那个 `_ipv4_candidates()` 里要是有个网段字符串写错了，真身会抛 ValueError，
    而人看到的就是"双击启动闪一下就没了"。所以这里降级成"不提示"，
    同时让 `smoke.py` 直接测真身 —— 写错了在自检里报出来，而不是留给用户去踩。
    """
    try:
        return _ipv4_candidates()
    except Exception:
        return []


def _port_has_listener(host: str, port: int) -> bool:
    """端口上是不是已经有人在服务了（真连一下，连得上才算）。

    为什么不能只看 bind 报不报错：Windows 上 `allow_reuse_address=1`（http.server 的默认）
    允许**第二个进程绑同一个端口**，Linux 会直接拒。于是"端口被占"这个错在 Windows 上
    根本不会出现 —— 两个服务抢一个端口，请求全归后绑的那个，之前那个窗口还在傻等，
    用户看到的是"我改了代码怎么没生效"。真连一下就没有这个盲区：
    TIME_WAIT 里的旧 socket 不会 accept，所以正常重启不会误报。
    """
    probe = "127.0.0.1" if host in WILDCARD_HOSTS else host
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect((probe, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _reachable_urls(host: str, port: int) -> tuple[str, list[str]]:
    """把「绑在哪儿」翻译成「从哪儿能打开」：返回 (主地址, 局域网地址列表)。

    绑了所有网卡时主地址用 127.0.0.1（本机肯定连得上），再附上枚举到的局域网地址 ——
    绑 0.0.0.0 的用处本来就是想让手机/平板也能开，那就把该敲的地址直接给出来。
    指定了具体地址（--host 192.168.x.x）就照实报，不替用户改。
    """
    if host in WILDCARD_HOSTS:
        ips = _local_ipv4s()
        return f"http://127.0.0.1:{port}/", [f"http://{ip}:{port}/" for ip in ips]
    return f"http://{host}:{port}/", []


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--keep", choices=("1", "0"), default=None,
                    help=U.t("srv.arg.keep"))
    ap.add_argument("--lang", choices=list(U.LANGS), default=None,
                    help=U.t("lang.arghelp"))
    ap.add_argument("--open", dest="open_browser", action="store_true",
                    help=U.t("srv.arg.open"))
    a = ap.parse_args()

    # 语言要在任何一句话之前定下来 —— 后面每条提示都按它取词条
    if a.lang:
        U.i18n.set_lang(a.lang)

    global CFG
    CFG = load_settings()
    if a.keep is not None:
        CFG["keep"] = a.keep == "1"
        save_settings()

    # 1. 先把老版本的散文件收进各自的工程文件夹（幂等，第二次就没得收了）
    st = migrate_layout()
    if st["runs"]:
        tail = U.t("srv.migrate.tail", n=st["leftover"]) if st["leftover"] else ""
        print(U.t("srv.migrate.done", runs=st["runs"], files=st["files"], tail=tail))
    if st["failed"]:
        print(U.t("srv.migrate.failed", n=st["failed"]))

    threading.Thread(target=worker, daemon=True).start()
    n = restore_jobs()

    # 先把端口占住，再说"已启动"。反过来的话端口被占时会先打一行"已启动 http://…"、
    # 紧接着才报起不来 —— 那行假消息最容易把人带沟里。
    # （Windows 上它甚至不会报错，见 _port_has_listener 的说明。）
    busy = _port_has_listener(a.host, a.port)
    try:
        srv = http.server.ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        sys.exit(U.t("srv.fail", port=a.port, err=e, alt=a.port + 1))
    srv.daemon_threads = True

    if busy:
        print(U.t("srv.port.busy", port=a.port))
        print(U.t("srv.port.busy2"))
        print(U.t("srv.port.busy3"))

    base_url, lan_urls = _reachable_urls(a.host, a.port)
    print(U.t("srv.started", url=base_url))
    if lan_urls:
        print(U.t("srv.lan", urls="  ".join(lan_urls)))
        print(U.t("srv.lan.note"))
    print_model_report(model_report())
    print(U.t("srv.workdir", dir=WORK_DIR)
          + (U.t("srv.workdir.jobs", n=n) if n else "")
          + (U.t("srv.workdir.link") if n else ""))
    print(U.t("srv.keep", mode=U.t("srv.keep.on") if CFG["keep"] else U.t("srv.keep.off")))
    if not CFG["keep"]:
        print(U.t("srv.keep.note", path=SETTINGS_PATH))
    print(U.t("srv.ctrl_c"))

    if a.open_browser:
        # 已经 bind + listen 过了，所以现在开浏览器不会扑空：
        # 那次请求会先排在内核的等待队列里，等 serve_forever 起来就有人接。
        # 不自己按秒数猜延迟 —— 猜短了用户看到一个「无法访问」的页面，
        # 还以为是我们坏了。开不起来（没有桌面环境之类）也只是提示一句，不影响服务。
        url = base_url
        try:
            webbrowser.open(url)
            print(U.t("srv.opened", url=url))
        except Exception as e:
            print(U.t("srv.open_fail", err=type(e).__name__, url=url))

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if not CFG["keep"]:
            k = wipe_work()
            print("\n" + U.t("srv.wiped", n=k))
        else:
            print("\n" + U.t("srv.stopped", dir=WORK_DIR))


if __name__ == "__main__":
    main()
