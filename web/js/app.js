/* ============================================================================
   BigPixels 放大台 · 全部行为

   页面骨架在 ../index.html，样式在 ../css/app.css，这里只有脚本。
   服务端按 /js/ 前缀把本文件发出去（见 server.py 的 STATIC_OK）。
   没有构建步骤，也没有框架：浏览器直接跑这一份。
   ============================================================================ */
"use strict";
const $ = s => document.querySelector(s);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const round2 = v => Math.round(v * 100) / 100;

/* 放大镜倍率的单位是「屏幕像素 : 输出像素」。
   1:1 就是输出一像素对屏幕一像素 —— 这一格看到的才是模型的原始像素，
   另外两格是同一块画面上不同算法的结果，尺寸被压到一样，比的就是信息密度。
   比 1:1 更远的档位（1:2 / 1:4 / 1:8）是「一格装下一大块画面」：想看清整块结构、
   不想盯着单个像素看的时候用。
   倍率有两个入口，指的是同一件事：连续滑块负责「随便找个合适的」，
   五个预设档负责「一键跳到熟悉的刻度」。两边读写的都是 S.zoom，改一个另一个跟着动。*/
const ZOOMS = [0.125, 0.25, 0.5, 1, 2];   // 预设档位：1:8 / 1:4 / 1:2 / 1:1 / 2:1
const SPAN_CAP = 0.6;        // 一格最多装下画面短边的六成 —— 留四成余量，框框才跟着鼠标跑
const ZOOM_MAX = 2;          // 最近那一端：一格放四个输出像素，再近就只剩插值噪声了
const zoomLabel = z => z >= 1 ? round2(z) + ":1" : "1:" + Math.round(1 / z);
const paneW = () => Math.max(48, Math.round($("#paneIn").clientWidth) || 200);
const PANES = [["#imIn", "input", "#paneIn"], ["#imBic", "bicubic", "#paneBic"],
               ["#imAi", "result", "#paneAi"]];
const TILE_MARGIN = 2.2;     // 一次要一块比视窗大 2.2 倍的，鼠标在里面挪就不用再请求
const TILE_MAX = 4096;       // 跟服务端 TILE_REQ_MAX 对齐：单次取块的边长上限
const NOISE_PRESETS = ["art", "art-hd", "photo"];
const OUT_MP_WARN = 64, OUT_MP_STOP = 160;

/* 触摸设备（手机 / 平板）：没有 hover，也没有鼠标。
   所有「把鼠标放到图上」「拖入图片」的说法在这类设备上都是错的，
   得换成「点一下」「点这里选图」。文案分两处：
     · HTML 里另挂一套，写在 data-touch 属性上，由 applyTouchCopy() 抄进去；
     · JS 现拼的用 T(鼠标说法, 触摸说法) 当场二选一。
   桌面那一套原样不动 —— 两边说的都得是实话。
   判据用 (pointer:coarse) 而不是宽度：带触摸屏的笔记本主指针仍是鼠标，
   不该被当成手机。 */
const TOUCH = matchMedia("(pointer:coarse)").matches;
const T = (mouse, touch) => (TOUCH ? touch : mouse);
function applyTouchCopy() {
  if (!TOUCH) return;
  document.querySelectorAll("[data-touch]").forEach(el => {
    el.textContent = el.dataset.touch;
  });
}

const S = {
  cfg: null,
  src: null,        // {url,name,w,h,blob?,bytes?,revoke?}
  jobId: null,
  result: null,
  timer: null,
  ticks: 0,
  sel: {preset: "anime", scale: 4, denoise: "medium", clear: "normal", bright: "off", tile: 256},
  split: 50,
  zoom: 1,
  zoomOn: null,     // 当前选中的预设档（拖过滑块就是 null —— 这时没有哪个档位被选中）
  keep: true,       // 结果去向：true 保存（默认）/ false 暂存
  at: {x: .5, y: .5},
  busy: false,
  view: null,       // 放大镜当前已经拿到手的那一块（输出坐标）
  pending: null,    // 正在取的下一块
  tok: 0,           // 取块序号：慢的那个回来了也不许覆盖新的
  live: null        // 最近一次算出来的视窗几何
};

const fmtBytes = n => {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(2) + " MB";
};
const mp = (w, h) => w * h / 1e6;
const dnLabel = id => {
  const d = (S.cfg && S.cfg.denoise || []).find(x => x.id === id);
  return d ? d.label : id;
};

/* ---------------------------------------------------------- 反馈 */
function fail(msg, detail) {
  const el = $("#err");
  el.innerHTML = "<strong>没做成</strong><span>" + msg + "</span>" +
                 (detail ? "<code>" + detail + "</code>" : "");
  el.hidden = false;
  $("#prog").hidden = true;
  $("#overlay").hidden = true;
  S.busy = false;
  $("#go").disabled = !S.src;
  $("#go").textContent = S.result ? "重新放大" : "开始放大";
}
const clearErr = () => { $("#err").hidden = true; };

/* ---------------------------------------------------------- 主题 */
const THEMES = [["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]];
function applyTheme(t) {
  const r = document.documentElement;
  if (t === "system") { r.removeAttribute("data-theme"); localStorage.removeItem("bigpixels.theme"); }
  else { r.dataset.theme = t; localStorage.setItem("bigpixels.theme", t); }
  const hit = THEMES.find(x => x[0] === t) || THEMES[0];
  $("#themeTxt").textContent = hit[1];
}
$("#themer").onclick = () => {
  const cur = localStorage.getItem("bigpixels.theme") || "system";
  const i = THEMES.findIndex(x => x[0] === cur);
  applyTheme(THEMES[(i + 1) % THEMES.length][0]);
};

/* ---------------------------------------------------------- 单选项组 */
/* 用真的 radio：方向键切换、Tab 只在组内停一次，都是浏览器白送的，
   比手搓 role="radio" 可靠。选中态靠 .on 类，不依赖 :has()。 */
function radioGroup(el, name, items, selId, make, onPick) {
  el.innerHTML = "";
  items.forEach(it => {
    const id = name + "-" + it.id;
    const lab = document.createElement("label");
    lab.className = el.dataset.kind || "opt";
    lab.htmlFor = id;
    lab.dataset.id = it.id;
    if (it.title) lab.title = it.title;
    if (it.disabled) lab.classList.add("off");
    if (String(it.id) === String(selId)) lab.classList.add("on");
    lab.innerHTML = '<input type="radio" name="' + name + '" id="' + id + '" value="' + it.id + '"' +
      (String(it.id) === String(selId) ? " checked" : "") +
      (it.disabled ? " disabled" : "") + ">" + make(it);
    el.appendChild(lab);
  });
  el.onchange = e => {
    if (e.target.name !== name) return;
    [...el.children].forEach(c => c.classList.toggle("on", c.dataset.id === e.target.value));
    onPick(e.target.value);
  };
}

/* ---------------------------------------------------------- 引擎状态条 */
function renderEngine(c) {
  const dev = Object.values(c.device_probe || {});
  const hasDml = c.providers.some(p => /DML|DirectML/i.test(p));
  const dmlOk = dev.includes("dml");
  $("#engine").innerHTML = '<span class="chip"><i class="dot' + (dmlOk ? "" : " warn") +
    '"></i>在本机 <b>' + (dmlOk ? "GPU + CPU" : "CPU") + " 上推理</b></span>";
  $("#tileNote").textContent = "显存或内存吃紧就调小。块与块之间重叠 " + c.overlap + " px 用来消除接缝。";
  $("#engineNote").textContent =
    (hasDml
      ? "这台机器有 DirectML，但 waifu2x 系权重在它上面会算出错误结果，程序在装载时会做一次 GPU/CPU 数值比对，" +
        "不一致就自动退回 CPU；Real-ESRGAN 与 AnimeSharp 留在 GPU 上跑。"
      : "这台机器上没有 DirectML，全部走 CPU。") +
    " 可用 provider：" + c.providers.join(" / ") + " · CPU " + c.cpus + " 核。";
}

/* ---------------------------------------------------------- 模型自检 */
/* 启动时服务端已经把每个预设要用的权重挨个点过一遍。缺了就把缺哪些、怎么装
   一并摆出来 —— 走 hf-mirror 镜像，慢或连不上再开代理。 */
function renderModelWarn(c) {
  const m = c.models || {};
  const box = $("#modelWarn");
  if (!m.missing || !m.missing.length) {
    box.hidden = true;
    return;
  }
  const ready = c.presets.filter(p => p.ready).length;
  box.hidden = false;
  box.innerHTML = "<strong>模型文件不全，只有 " + ready + " / " + c.presets.length +
    " 档能用</strong><span>缺这 " + m.missing.length + " 个权重（在 " + esc(m.dir) + "）：</span>" +
    "<code>" + m.missing.map(esc).join("<br>") + "</code>" +
    "<span>在项目目录里跑这个把它们拉下来 ——</span><code>" + esc(m.hint) + "</code>" +
    "<span>走的是 hf-mirror 镜像；速度很慢或者连不上，就先开代理再跑一次。</span>";
}

/* ---------------------------------------------------------- 保存 / 暂存 */
/* 默认保存：结果就留在工程文件夹里，服务一条都不自动删。
   切到暂存：结果只在服务运行期间留着，停了就清空 —— 所以完成后会盯着你下载。 */
const KEEPS = [{id: "keep", label: "保存"}, {id: "temp", label: "暂存"}];
function renderKeep(keep) {
  S.keep = keep !== false;
  radioGroup($("#keepMode"), "keep", KEEPS, S.keep ? "keep" : "temp",
    it => it.label, id => setKeep(id === "keep"));
  paintKeep();
}
function paintKeep() {
  const k = S.keep, work = (S.cfg && S.cfg.work_rel) || "outputs/web/";
  $("#keepNote").textContent = k
    ? "结果存进 " + work + "<时间戳>_<图名>_<任务号>/ ，服务不做任何自动清理。"
    : "结果只算暂存：服务一停就清空整个工程目录，界面上也不会替你留 —— 出图后记得先下载。";
  const d = (S.cfg && S.cfg.disk) || {};
  const used = (d.used || 0) / 1048576;
  const n = d.jobs || 0, cap = 16;
  $("#diskNote").textContent = k
    ? "现在已占 " + (used < 1024 ? used.toFixed(0) + " MB" : (used / 1024).toFixed(2) + " GB") +
      " / " + n + " 次工程，不清理。攒多了自己去那个目录删文件夹就行。"
    : "暂存模式最多留 " + fmtBytes((S.cfg && S.cfg.limits && S.cfg.limits.disk_temp) || 1572864000) +
      " 或 " + cap + " 次，超了按时间从最早的开始清。" +
      (n > cap ? " 现在就有 " + n + " 次 —— 下一次出图会先把最早的 " + (n - cap) + " 次清掉。" : "");
}
async function setKeep(keep) {
  try {
    const r = await fetch("/api/settings?keep=" + (keep ? "1" : "0"), {method: "POST"});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    S.keep = j.keep !== false;
    paintKeep();
    if (S.result) showSaveNote(S.result);
  } catch (e) {
    fail("设置没改成。", String(e.message || e));
  }
}
const esc = s => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/* ---------------------------------------------------------- 完成后的去向提醒 */
/* 「结果会留着还是会被删」是用户最需要当场知道的一件事，别藏在设置里。
   保存模式用中性色说清路径；暂存模式才该是提醒色。 */
function showSaveNote(j) {
  const box = $("#saveNote");
  const path = (S.cfg && S.cfg.work_rel) || "outputs/web/";
  const rel = String(j.rel || "");
  const folder = rel.split("/").filter(Boolean).pop() || "";
  if (j.keep === false || !S.keep) {
    box.className = "notice";
    box.hidden = false;
    box.innerHTML = "<b>暂存模式：这份结果不会留盘。</b>服务一停，" + esc(path) +
      " 里的工程文件夹就一起清掉了 —— 现在按右边的「下载」存一份到自己想放的地方。";
  } else {
    box.className = "notice ok";
    box.hidden = false;
    box.innerHTML = "结果已经存进 <b>" + esc(path) + "</b>" +
      (folder ? "<code>" + esc(folder) + "/</code>" : "") +
      "，不会被自动清理。要留档建议还是下载一份拿走。";
  }
}

/* ---------------------------------------------------------- 输入 */
function setSource(src) {
  if (S.src && S.src.revoke && S.src.url !== src.url) URL.revokeObjectURL(S.src.url);
  S.src = src;
  S.result = null;
  try { history.replaceState(null, "", location.pathname); } catch (e) {}
  $("#drop").hidden = true;
  $("#loaded").hidden = false;
  $("#thumb").src = src.url;
  $("#fname").textContent = src.name;
  $("#fdim").textContent = src.w + " × " + src.h + "  ·  " + mp(src.w, src.h).toFixed(2) + " MP" +
                           (src.bytes ? "  ·  " + fmtBytes(src.bytes) : "");
  $("#viewport").style.setProperty("--ar", src.w + " / " + src.h);
  $("#viewport").style.setProperty("--arn", src.w / src.h);
  $("#imgBefore").src = src.url;
  $("#imgAfter").removeAttribute("src");
  $("#emptyState").hidden = true;
  ["#cornerL", "#cornerR", "#divider", "#grip", "#cross"].forEach(s => { $(s).hidden = true; });
  $("#loupe").hidden = true;
  $("#ledgerWrap").hidden = true;
  $("#saveNote").hidden = true;
  $("#dl").setAttribute("aria-disabled", "true");
  $("#dlText").textContent = "下载结果";
  $("#fs").setAttribute("aria-disabled", "true");
  S.ptr = null;
  S.view = null;
  S.pending = null;
  S.tok++;
  $("#stageNote").textContent = T(
    "结果出来后，把鼠标放到图上，下面的放大镜会跟上。",
    "结果出来后，点一下图上看哪儿，下面的放大镜就跟到那一块。");
  clearErr();
  $("#go").disabled = false;
  $("#go").textContent = "开始放大";
  updateReadout();
}

function dropSource() {
  if (S.src && S.src.revoke) URL.revokeObjectURL(S.src.url);
  S.src = null;
  S.result = null;
  $("#loaded").hidden = true;
  $("#drop").hidden = false;
  $("#imgBefore").removeAttribute("src");
  $("#imgAfter").removeAttribute("src");
  $("#emptyState").hidden = false;
  ["#cornerL", "#cornerR", "#divider", "#grip", "#cross"].forEach(s => { $(s).hidden = true; });
  $("#loupe").hidden = true;
  $("#ledgerWrap").hidden = true;
  $("#saveNote").hidden = true;
  $("#dl").setAttribute("aria-disabled", "true");
  $("#fs").setAttribute("aria-disabled", "true");
  S.ptr = null;
  S.view = null;
  S.pending = null;
  S.tok++;
  $("#go").disabled = true;
  $("#go").textContent = "开始放大";
  clearErr();
  updateReadout();
}

function acceptFile(f) {
  if (!f) return;
  if (!/^image\//.test(f.type || "")) {
    return fail("这不是图片文件。", "选 PNG / JPG / WEBP / TIFF，或者把图片文件拖进来。");
  }
  const cap = (S.cfg && S.cfg.limits.max_upload) || 41943040;
  if (f.size > cap) {
    return fail("图片太大，超过单张上限。",
                "上限 " + fmtBytes(cap) + "，当前 " + fmtBytes(f.size) + "。先裁小或压缩一下再试。");
  }
  const url = URL.createObjectURL(f);
  const im = new Image();
  im.onload = () => setSource({url, name: f.name, w: im.naturalWidth, h: im.naturalHeight,
                               blob: f, bytes: f.size, revoke: true});
  im.onerror = () => {
    URL.revokeObjectURL(url);
    fail("这个文件解不开。", "可能后缀和内容不符，或编码方式不支持。换成 PNG / JPG 再试。");
  };
  im.src = url;
}

$("#drop").onclick = () => $("#file").click();
$("#drop").onkeydown = e => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#file").click(); }
};
$("#file").onchange = e => { acceptFile(e.target.files[0]); e.target.value = ""; };
$("#replace").onclick = () => $("#file").click();
$("#clear").onclick = dropSource;
["dragenter", "dragover"].forEach(t => $("#drop").addEventListener(t, e => {
  e.preventDefault(); $("#drop").classList.add("over");
}));
["dragleave", "drop"].forEach(t => $("#drop").addEventListener(t, e => {
  e.preventDefault(); $("#drop").classList.remove("over");
}));
$("#drop").addEventListener("drop", e => acceptFile(e.dataTransfer.files[0]));
window.addEventListener("dragover", e => e.preventDefault());
window.addEventListener("drop", e => e.preventDefault());

/* ---------------------------------------------------------- 示例图 */
function renderSamples(c) {
  const list = c.samples || [];
  $("#samples").hidden = !list.length;
  const row = $("#sampleRow");
  row.innerHTML = "";
  list.forEach(s => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sample";
    b.title = s.note || s.name;
    b.innerHTML = '<img src="' + s.url + '" alt="" loading="lazy">' +
                  "<b>" + s.name + "</b><code>" + s.w + "×" + s.h + "</code>";
    b.onclick = async () => {
      clearErr();
      try {
        const r = await fetch(s.url);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const blob = await r.blob();
        setSource({url: s.url, name: s.id + ".png", w: s.w, h: s.h, blob, bytes: blob.size});
      } catch (err) {
        fail("取不到示例图。", String(err.message || err));
      }
    };
    row.appendChild(b);
  });
}

/* ---------------------------------------------------------- 三块选项 */
function renderPresets(c) {
  // 选中的那档权重不在（缺文件）就自动落到第一个能用的，不然「开始放大」按下去只会报错。
  const cur = c.presets.find(p => p.id === S.sel.preset);
  if (!cur || !cur.ready) {
    const ok = c.presets.find(p => p.ready);
    if (ok) S.sel.preset = ok.id;
  }
  radioGroup($("#presets"), "preset",
    c.presets.map(p => ({id: p.id, disabled: !p.ready, p, title: p.tech})),
    S.sel.preset,
    it => '<span class="box"></span><span class="txt"><b>' + it.p.label + "</b><small>" +
          it.p.desc + "</small></span><em>" +
          (it.p.ready ? it.p.scale + "×" : "缺权重 " + it.p.have + "/" + it.p.total) + "</em>",
    id => { S.sel.preset = id; applyPresetRules(); clearErr(); });
  const ready = c.presets.filter(p => p.ready).length;
  $("#modelCount").textContent = ready + " / " + c.presets.length + " 个可用";
  applyPresetRules();
}

function applyPresetRules() {
  const has = NOISE_PRESETS.includes(S.sel.preset);
  const box = $("#denoise");
  box.classList.toggle("off", !has);
  [...box.children].forEach(lab => {
    lab.classList.toggle("off", !has);
    const inp = lab.querySelector("input");
    if (inp) inp.disabled = !has;
  });
  $("#denoiseNote").hidden = has;
  $("#denoiseNote").textContent = has ? "" :
    "Real-ESRGAN 与 AnimeSharp 各自只有一份权重、不带降噪档，这一项对它们不起作用。";
}

function renderScales(c) {
  radioGroup($("#scales"), "scale", c.scales.map(s => ({id: s})), S.sel.scale,
    it => it.id + "×", id => { S.sel.scale = +id; updateReadout(); });
}

function renderDenoise(c) {
  radioGroup($("#denoise"), "denoise", c.denoise, S.sel.denoise,
    it => it.label, id => { S.sel.denoise = id; });
}

/* 清晰度：收尾锐化的强弱。原样 = 完全不锐化，更锐 = 线条最硬（接近线上那种观感）。
   注意 id 用 clarity 不是 clear —— #clear 已经是「移除」那个按钮了，撞了名字
   会把这个单选框整个渲染进源图卡片里。 */
function renderClear(c) {
  if (!c.clear) return;
  radioGroup($("#clarity"), "clear", c.clear, S.sel.clear,
    it => it.label, id => { S.sel.clear = id; });
}

function clearLabel(id) {
  const it = (S.cfg && S.cfg.clear || []).find(x => x.id === id);
  return it ? it.label : id;
}

/* 明暗：一个开关，不是技术参数 —— 默认「原样」最贴原图，
   「提亮」整体乘 1.019，换线上 bigjpg 那种通透感。 */
function renderBright(c) {
  if (!c.bright) return;
  radioGroup($("#bright"), "bright", c.bright, S.sel.bright,
    it => it.label, id => { S.sel.bright = id; });
}

function brightLabel(id) {
  const it = (S.cfg && S.cfg.bright || []).find(x => x.id === id);
  return it ? it.label : id;
}

/* 档位是「想要的倍率」，实际能放出多大还要看图片尺寸和取块边长上限 ——
   一格装不下的区域服务端不肯给。所以标签按实际能做到的倍率写。 */
/* 一格能装下多大一块，是被两头夹住的：
   右头是用户按的倍率（屏幕一格 = 输出一像素 × z），左头有两个闸门 ——
   单次取块的边长上限，以及**画面短边的六成**。
   六成这个闸门是必须的：区域一旦顶到整幅图，框框就再没地方挪了，
   鼠标推到哪儿它都停在原地（用户报过这个："卡这里过不去了"）。
   留四成余量，框框永远跟得动。 */
function loupeCap(j) {
  return Math.min(TILE_MAX / TILE_MARGIN, SPAN_CAP * Math.min(j.out_w, j.out_h));
}

function regionOf(z, j) {
  const P = paneW();
  const V = Math.max(1, Math.min(P / z, loupeCap(j)));
  return {P: P, V: V, z: P / V};
}

/* 倍率标签要写实际能做到的：按不到 1:8 就写 1:7，别写个假的 */
function effZoom(z) {
  const j = S.result;
  return j ? regionOf(z, j).z : z;
}

/* 滑块两端：左边最远（一格装下最多画面）、右边最近（一格放到最大）。
   两端都由这张图实际能算出来的范围决定 —— 左边就是「短边六成」那道闸门，
   右边就是 ZOOM_MAX。定死 0.125..2 的话，小图上左半截全是死的，拖了没反应。 */
function zoomRange(j) {
  if (!j) return {zMin: 0.125, zMax: ZOOM_MAX};
  const P = paneW();
  const zMin = Math.min(P / loupeCap(j), ZOOM_MAX);
  return {zMin: zMin, zMax: ZOOM_MAX};
}

/* 默认看多大：格宽的四倍，但最多不超过画面短边的一半。
   大图（68 MP）上这就是 1:4 —— 一上来就该看得见画面，而不是一格糊糊的像素；
   小图上四倍格宽比整张图还大，退回半幅，免得框框一上来就顶死没法挪。 */
function defaultZoom(j) {
  const r = zoomRange(j);
  const V = Math.min(4 * paneW(), 0.5 * Math.min(j.out_w, j.out_h));
  return clamp(paneW() / V, r.zMin, r.zMax);
}

const zoomPos = z => {                       // 倍率 → 滑块位置（对数刻度，两端都跑得动）
  const r = zoomRange(S.result);
  const t = Math.log(z / r.zMin) / Math.log(r.zMax / r.zMin || 2);
  return Math.round(clamp(t, 0, 1) * 1000);
};
const posZoom = p => {
  const r = zoomRange(S.result);
  return r.zMin * Math.pow(r.zMax / r.zMin || 2, clamp(p, 0, 1000) / 1000);
};

/* 滑块和档位键指的是同一件事，改哪个都要把另一个同步上 */
function setZoom(z, from) {
  S.zoom = z;
  S.zoomOn = from === undefined ? null : from;
  S.view = null; S.pending = null; S.tok++;   // 换了尺度，手上那块作废
  renderSpan();
  if (S.result) { updateLoupe(); renderZoom(); }
}

function renderSpan() {
  if (!S.result) return;
  $("#span").value = zoomPos(S.zoom);
  $("#spanOut").textContent = zoomLabel(effZoom(S.zoom));
}

function renderZoom() {
  /* 最远那几档被闸门夹住时会算出同一个倍率 —— 并排摆两个写着一样数字的按钮没意义，
     只留看得更远的那个。滑块能连续调，所以被夹住的档位不选中是正常的，不硬塞。 */
  const seen = new Set(), items = [];
  ZOOMS.forEach(z => {
    const lab = zoomLabel(effZoom(z));
    if (seen.has(lab)) return;
    seen.add(lab);
    items.push({id: z, label: lab});
  });
  radioGroup($("#zoom"), "zoom", items, S.zoomOn,
    it => it.label,
    id => setZoom(+id, +id));
}

function renderTiles(c) {
  const sel = $("#tile");
  sel.innerHTML = "";
  c.tiles.forEach(t => {
    const o = document.createElement("option");
    o.value = t;
    o.textContent = t + " px";
    sel.appendChild(o);
  });
  sel.value = S.sel.tile;
  sel.onchange = () => { S.sel.tile = +sel.value; };
}

function updateReadout() {
  const out = $("#readout"), warn = $("#sizeWarn");
  if (!S.src) {
    out.innerHTML = "<span>载入图片后这里会算出输出尺寸</span>";
    warn.hidden = true;
    return;
  }
  const w = S.src.w * S.sel.scale, h = S.src.h * S.sel.scale, m = mp(w, h);
  out.innerHTML =
    "<b>" + S.src.w + " × " + S.src.h + "</b><span class=\"arrow\">→</span>" +
    "<b>" + w.toLocaleString("zh-CN") + " × " + h.toLocaleString("zh-CN") + "</b>" +
    "<span>·</span><span>" + mp(S.src.w, S.src.h).toFixed(2) + " → " + m.toFixed(1) + " MP</span>";
  if (m >= OUT_MP_STOP) {
    warn.hidden = false;
    warn.innerHTML = "输出会到 <b>" + m.toFixed(0) + " MP</b>（" + w.toLocaleString("zh-CN") + " × " +
      h.toLocaleString("zh-CN") + "），这台机器跑不动。换小一点的倍率，或者先把原图裁小 —— 上限 " +
      OUT_MP_STOP + " MP。";
  } else if (m >= OUT_MP_WARN) {
    warn.hidden = false;
    warn.innerHTML = "输出 " + m.toFixed(0) + " MP 偏大，内存和时间都要多花不少。";
  } else {
    warn.hidden = true;
  }
  $("#go").disabled = S.busy || m >= OUT_MP_STOP;
}

/* ---------------------------------------------------------- 提交与轮询 */
async function sourceBlob() {
  if (S.src.blob) return S.src.blob;
  const r = await fetch(S.src.url);
  if (!r.ok) throw new Error("读不到源文件（HTTP " + r.status + "）");
  return await r.blob();
}

function setProg(pct, stage, model) {
  const p = clamp(Math.round(pct || 0), 0, 100);
  $("#fill").style.width = p + "%";
  $("#ruleBar").setAttribute("aria-valuenow", p);
  $("#ovPct").textContent = p + "%";
  $("#progStage").textContent = stage || "处理中";
  $("#ovLbl").textContent = stage || "处理中";
  if (model != null) $("#progModel").textContent = model;
}

function markPasses(n) {
  if (n === S.ticks) return;
  S.ticks = n;
  const t = $("#ticks");
  t.innerHTML = "";
  for (let i = 1; i < n; i++) {
    const mark = document.createElement("i");
    mark.style.left = (100 * i / n) + "%";
    t.appendChild(mark);
  }
}

async function start() {
  if (!S.src || S.busy) return;
  clearErr();
  S.busy = true;
  S.result = null;
  S.ticks = 0;
  $("#go").disabled = true;
  $("#go").textContent = "放大中…";
  $("#loupe").hidden = true;
  $("#ledgerWrap").hidden = true;
  $("#saveNote").hidden = true;
  $("#prog").hidden = false;
  $("#overlay").hidden = false;
  setProg(0, "正在把图交给引擎", "");
  $("#live").textContent = "开始处理";

  try {
    const blob = await sourceBlob();
    const q = new URLSearchParams({preset: S.sel.preset, scale: S.sel.scale,
                                  denoise: S.sel.denoise, clear: S.sel.clear,
                                  bright: S.sel.bright,
                                  tile: S.sel.tile, name: S.src.name});
    const r = await fetch("/api/job?" + q, {method: "POST", body: blob});
    const j = await r.json().catch(async () => ({error: await r.text()}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    S.jobId = j.id;
    $("#jobId").textContent = j.id;
    S.timer = setInterval(poll, 350);
    poll();
  } catch (e) {
    S.busy = false;
    fail("任务没能提交上去。", String(e.message || e));
  }
}
$("#go").onclick = start;

async function poll() {
  try {
    const r = await fetch("/api/job/" + S.jobId);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    if (j.state === "error") {
      clearInterval(S.timer);
      S.busy = false;
      return fail("引擎报错了。", j.msg || "");
    }
    if (j.passes) markPasses(j.passes);
    const info = [j.model, j.device, j.passes > 1 ? "串联 " + j.passes + " 趟" : null]
      .filter(Boolean).join("  ·  ");
    setProg(j.progress, j.stage, info);
    $("#progTime").textContent = (j.elapsed || 0).toFixed(1) + " s";
    if ($("#live").textContent !== j.stage) $("#live").textContent = j.stage || "";
    if (j.state === "done") {
      clearInterval(S.timer);
      S.busy = false;
      S.result = j;
      showResult(j);
    }
  } catch (e) {
    clearInterval(S.timer);
    S.busy = false;
    fail("查任务状态时断了。", String(e.message || e));
  }
}

/* ---------------------------------------------------------- 结果 */
function showResult(j) {
  $("#prog").hidden = true;
  $("#overlay").hidden = true;
  const base = "/api/job/" + j.id;

  const after = $("#imgAfter");
  // 光台上只放缩略预览：68 MP 的原始 PNG 交给浏览器，光是解码就够它卡一阵了。
  // 真要抠像素有放大镜，那条路是按需取块，一次几十 KB。
  after.src = base + "/result?view=1";
  after.classList.remove("develop");
  void after.offsetWidth;      // 逼一次重排，让动画能重放
  after.classList.add("develop");

  $("#imgBefore").src = base + "/input?view=1";
  $("#cornerL").hidden = false;
  $("#cornerR").hidden = false;
  $("#cornerL").textContent = "原图 " + j.in_w + "×" + j.in_h;
  $("#cornerR").textContent = "AI " + j.out_w + "×" + j.out_h;
  $("#divider").hidden = false;
  $("#grip").hidden = false;
  setSplit(50);

  const stem = (S.src ? S.src.name : "result").replace(/\.[^.]+$/, "");
  const dl = $("#dl");
  dl.href = base + "/result";
  dl.setAttribute("download", stem + "_" + j.scale + "x_ai.png");
  dl.setAttribute("aria-disabled", "false");
  $("#dlText").textContent = "下载 " + j.out_w + "×" + j.out_h + " PNG";

  $("#stageNote").textContent =
    "拖竖线分开对比，想放大看就按「全屏对比」。光台上是缩略预览，像素级对照看下面的放大镜。";
  $("#loupe").hidden = false;
  $("#ledgerWrap").hidden = false;
  $("#fs").setAttribute("aria-disabled", "false");
  prepareLoupe(j, base);
  renderLedger(j);
  showSaveNote(j);

  $("#go").textContent = "重新放大";
  $("#go").disabled = false;
  updateReadout();
  $("#live").textContent = "处理完成";
  // 把任务号写进地址栏：刷新不丢结果，也能直接把这一份发给别人
  try { history.replaceState(null, "", location.pathname + "?job=" + j.id); } catch (e) {}
}

/* ---------------------------------------------------------- 放大镜 */
function prepareLoupe(j, base) {
  /* 默认落点交给 defaultZoom 算（大图上是 1:4）：一上来就该看见画面，而不是一格糊糊的像素。
     如果这个落点正好压在某个预设档上，就把那一档选中 —— 省得用户一上来
     看着五个没选中的按钮，不知道自己在哪一档。 */
  S.zoom = defaultZoom(j);
  const zl = zoomLabel(effZoom(S.zoom));
  S.zoomOn = null;
  ZOOMS.forEach(z => { if (S.zoomOn === null && zoomLabel(effZoom(z)) === zl) S.zoomOn = z; });
  S.view = null;
  S.pending = null;
  S.tok++;
  S.at = {x: .5, y: .5};
  renderSpan();
  renderZoom();
  PANES.forEach(([sel]) => $(sel).removeAttribute("src"));
  $("#capIn").textContent = j.in_w + "×" + j.in_h;
  $("#capBic").textContent = j.out_w + "×" + j.out_h;
  $("#capAi").textContent = j.out_w + "×" + j.out_h;
  $("#bicNote").hidden = j.has_baseline;
  $("#bicNote").textContent = j.has_baseline ? "" :
    "中间这一格是当场算的 —— 只算你要看的这一小块，所以输出再大也有得比。" +
    "整幅双三次 PNG 落盘代价太大，这次没存，计量表里「体积」那一行就留空了。";
  updateLoupe();
}

/* 视窗尺寸随倍率走：屏幕一格 = 输出一像素 × zoom，所以一格能装下 P/zoom 个输出像素。
   V 不是直接取 P/zoom，还要被 loupeCap 夹一道（单次取块边长、画面短边六成）。
   卡住之后 z 必须按 V 反推（z = P/V），不能还用用户按的那个档位：
   下面 --tilew / --dx 的换算和发给服务端的显示尺寸都靠这个 z，
   对不上的话画面会错位、缩放也是假的。

   中心点不夹进图里 —— 就钉在鼠标底下。夹进去的话，视窗只能整个待在图内，
   鼠标推到画面边角时框框最多只能到离边 V/2 的地方，永远差一截
   （用户报过两条："鼠标都到原图文字了，框框没到"、"卡这里过不去了"）。
   视窗因此可能有一角伸到图外，那部分交给 region() 裁掉、取块只管图里那块，
   界面上露出来的底色就是图外的留白。 */
function loupeGeom() {
  const j = S.result;
  const W = j.out_w, H = j.out_h;
  const P = paneW();
  const V = Math.max(1, Math.min(P / S.zoom, loupeCap(j)));
  const cx = S.at.x * W, cy = S.at.y * H;
  return {W: W, H: H, P: P, z: P / V, V: V, cx: cx, cy: cy, vx: cx - V / 2, vy: cy - V / 2};
}

/* 视窗落在图里的那一部分。中心贴着边时视窗有一半在图外，
   取块、判「这块还罩得住吗」都只认图里这一块。
   两边都夹过，所以一定非空（视窗边长最多是短边六成，伸出去的那半截够不着对边）。 */
function region(g) {
  return {x0: Math.max(0, g.vx), y0: Math.max(0, g.vy),
          x1: Math.min(g.W, g.vx + g.V), y1: Math.min(g.H, g.vy + g.V)};
}

/* 手上这一块还罩得住视窗吗。只比图里那一块，另外两边留点余量 ——
   鼠标在里面挪一点就别再跑一趟了。留多少要看这块比视窗富裕多少：
   贴着图边的时候视窗只剩半块宽，按视窗边长去要余量就永远不满足、每帧重取；
   而已经贴住图边界的那一侧本来就没得挪，也不必留。 */
function tileHolds(t, g) {
  if (!t) return false;
  const r = region(g), m = g.V * .18;
  const ok = (lo, hi, tlo, thi, dim) => {
    const pad = Math.min(m, Math.max(0, (thi - tlo) - (hi - lo)) / 2);
    return lo >= tlo + (tlo <= 0 ? 0 : pad) - .5 &&
           hi <= thi - (thi >= dim ? 0 : pad) + .5;
  };
  return ok(r.x0, r.x1, t.x, t.x + t.w, g.W) &&
         ok(r.y0, r.y1, t.y, t.y + t.h, g.H);
}

function pickTile(g) {
  if (tileHolds(S.view, g)) return S.view;
  const T = Math.max(1, Math.min(Math.ceil(g.V * TILE_MARGIN), TILE_MAX));
  const tw = Math.min(T, g.W), th = Math.min(T, g.H);
  const tx = Math.round(clamp(g.cx - T / 2, 0, Math.max(0, g.W - tw)));
  const ty = Math.round(clamp(g.cy - T / 2, 0, Math.max(0, g.H - th)));
  return {x: tx, y: ty, w: Math.min(tw, g.W - tx), h: Math.min(th, g.H - ty)};
}

/* 一块 = 三个小请求，每个几十 KB。先解码完再换 src，免得中间空一帧闪一下。
   同一时刻只放一块上路：鼠标拖得比网速快的时候，那一块请求一直在飞，
   被作废再发是白烧 CPU。落地后统一再判一次，需要就补一块。

   注意落地必须无条件补画：第一次取块的时候 S.view 还是空的，
   updateLoupe 里那次 drawLoupe 直接返回了 —— 不补的话，
   刚出图、鼠标还没动过的时候三格是全黑的（这个坑真踩过）。 */
function fetchTile(t, z) {
  const tok = ++S.tok;
  S.pending = t;
  /* dw：这一块在屏幕上画多大。倍率比 1:1 更远时它比取块本身小得多，
     让服务端先缩到屏幕尺寸再发 —— 否则一大块原像素传过来、
     再让浏览器缩到一格里去，白传几十倍字节、白解一次码。
     1:1 时 dw 正好等于取块边长，服务端一个字都不动，
     那一档拿到的还是模型的原始像素。 */
  const dw = Math.max(48, Math.round(t.w * z));
  const q = "/api/job/" + S.jobId + "/detail?x=" + t.x + "&y=" + t.y +
            "&w=" + t.w + "&h=" + t.h + "&dw=" + dw + "&layer=";
  let left = PANES.length;
  const land = () => {
    if (--left > 0) return;
    S.pending = null;
    if (tok === S.tok) S.view = t;           // 三张都到齐了才认这块，免得混着显示
    updateLoupe();
  };
  PANES.forEach(p => {
    const sel = p[0], layer = p[1], win = p[2];
    const url = q + layer;
    const probe = new Image();
    probe.onload = () => {
      if (tok === S.tok) { $(win).classList.remove("busy"); $(sel).src = url; }
      land();
    };
    probe.onerror = () => { if (tok === S.tok) $(win).classList.add("busy"); land(); };
    $(win).classList.add("busy");
    probe.src = url;
  });
  setTimeout(() => {                          // 兜底：万一有一张永远不回来，别把放大镜锁死
    if (tok === S.tok && S.pending === t) { S.pending = null; updateLoupe(); }
  }, 5000);
}

/* 位置只动 transform，不动布局 —— 鼠标追着跑也不会掉帧。
   十字框标出放大镜正在看光台上的哪一块：框的左上角就是视窗左上角（vx/vy）。
   这儿必须是左上角而不是中心 —— 按中心摆的话整个框会往右下偏半格，
   看着就是「框框没跟到鼠标那儿」。视窗伸到图外时框也跟着出去，
   被光台的 overflow:hidden 裁掉半截，那正是它该有的样子。 */
function drawLoupe(g) {
  const t = S.view;
  if (!t || !g) return;
  const panes = $("#panes");
  panes.style.setProperty("--tilew", (t.w * g.z) + "px");
  /* dx/dy 会是正的：视窗伸到图外时，画面得往右/往下让出那段留白。
     别像以前那样夹到 ≤0 —— 夹了就等于把画面又拽回图里，「贴边」这件事就白做了。 */
  panes.style.setProperty("--dx", clamp((t.x - g.vx) * g.z, -t.w * g.z, t.w * g.z) + "px");
  panes.style.setProperty("--dy", clamp((t.y - g.vy) * g.z, -t.h * g.z, t.h * g.z) + "px");

  const cross = $("#cross");
  cross.hidden = false;
  cross.style.left = (g.vx / g.W * 100) + "%";
  cross.style.top = (g.vy / g.H * 100) + "%";
  cross.style.width = (g.V / g.W * 100) + "%";
  cross.style.height = (g.V / g.H * 100) + "%";
}

function updateLoupe() {
  const j = S.result;
  if (!j) return;
  const g = loupeGeom();
  S.live = g;
  // 手上那块罩不住视窗时才要新的；已经有一块在飞就等它落地，
  // 落地会再喊一次 updateLoupe（见 fetchTile），不必在这儿排队。
  if (!tileHolds(S.view, g) && !S.pending) fetchTile(pickTile(g), g.z);
  drawLoupe(g);

  const whole = Math.round(g.V), size = whole + "×" + whole;
  /* 这行说明也在跟光台抢同一屏的高度：原来 12.5 px 会折成两行、白占 39 px。
     字号在 app.css 里降到 11 px（一行放得下约 77 个全角字），文案也只留必要的：
     换来的 21 px 就是「光台和放大镜同屏」里的一份。改文案记得两边一起看。 */
  let head, tail = " 这块 " + size + "。";
  if (g.z > 1) {
    head = "一格 = 输出一像素 × " + round2(g.z) + "，看的是「AI 放大」的样子，不是它的真实像素。";
  } else if (g.z === 1) {
    head = "一格正好是一个输出像素（1:1）：多出来的细节是补的还是原来就有的，一比就清楚。";
  } else {
    head = "把 " + size + " 压进一格（约 1:" + Math.round(1 / g.z) + "），看整块结构；要抠像素按「1:1」。";
    tail = "";
  }
  $("#zoomNote").textContent = "三格同块同尺寸。" + head + tail;
}

/* pointermove 一秒能来好几百次，合并成每帧最多算一次 */
let rafId = 0;
function scheduleLoupe() {
  if (rafId) return;
  rafId = requestAnimationFrame(() => {
    rafId = 0;
    if (!S.result || !S.ptr) return;
    const r = $("#viewport").getBoundingClientRect();
    S.at = {x: clamp((S.ptr.x - r.left) / r.width, 0, 1),
            y: clamp((S.ptr.y - r.top) / r.height, 0, 1)};
    updateLoupe();
  });
}
$("#viewport").addEventListener("pointermove", e => {
  if (!S.result) return;
  S.ptr = {x: e.clientX, y: e.clientY};
  scheduleLoupe();
});

/* 触摸设备上光靠 pointermove 不够：手指点一下（没移动）根本不产生 pointermove，
   于是「点图上看细节」在手机上完全没反应 —— 放大镜永远停在画面正中。
   所以点按也要喂一次位置。拖动时 pointermove 照样会来，两条路汇到同一个 scheduleLoupe。

   这里**只**管放大镜，不碰分割线：桌面那个 pointerdown 会顺手把分割线拖到点击处，
   手指一点就跳太吓人了，所以那条规则刻意只认 pointerType==="mouse"，
   这里也把 mouse 排除掉，两边各管各的。 */
$("#viewport").addEventListener("pointerdown", e => {
  if (!S.result || e.pointerType === "mouse") return;
  if (e.target.closest("#grip")) return;      // 按的是分割线上的抓手，别抢
  S.ptr = {x: e.clientX, y: e.clientY};
  scheduleLoupe();
});

/* 取景滑块。拖动时不带 from，于是 S.zoomOn 清空 —— 倍率不再等于任何一个档位，
   档位那排就该一个都不亮（滑块本来就落在两档之间）。反过来点档位时
   setZoom(+id, +id) 会把滑块挪到对应位置，两边始终一致。 */
$("#span").addEventListener("input", e => {
  if (!S.result) return;
  setZoom(posZoom(+e.target.value), null);
});

/* 窗口一换尺寸，格子宽度就变，一格能装多少输出像素跟着变 ——
   倍率标签（页面上和全屏栏里各是一套宽度）得重算。合并到一帧里做，
   拖窗口的时候别每来一个事件就重建一遍那五个 radio。 */
let fitRaf = 0;
window.addEventListener("resize", () => {
  if (!S.result || fitRaf) return;
  fitRaf = requestAnimationFrame(() => {
    fitRaf = 0;
    if (!S.result) return;
    renderSpan();
    renderZoom();
    updateLoupe();
  });
});

/* ---------------------------------------------------------- 分割线 */
function setSplit(p) {
  S.split = clamp(p, 0, 100);
  $("#viewport").style.setProperty("--split", S.split + "%");
  $("#grip").setAttribute("aria-valuenow", Math.round(S.split));
  $("#grip").setAttribute("aria-valuetext", "原图露出 " + Math.round(S.split) + "%");
}
(function wireSplit() {
  let dragging = false, raf = 0, px = 0;
  const apply = () => {
    raf = 0;
    const r = $("#viewport").getBoundingClientRect();
    setSplit((px - r.left) / r.width * 100);
  };
  const from = x => { px = x; if (!raf) raf = requestAnimationFrame(apply); };

  $("#grip").addEventListener("pointerdown", e => {
    dragging = true; from(e.clientX); e.preventDefault();
    try { $("#grip").setPointerCapture(e.pointerId); } catch (err) {}
  });
  window.addEventListener("pointermove", e => { if (dragging) from(e.clientX); });
  window.addEventListener("pointerup", () => { dragging = false; });
  window.addEventListener("pointercancel", () => { dragging = false; });
  $("#viewport").addEventListener("pointerdown", e => {
    if (S.result && e.pointerType === "mouse" && !e.target.closest("#grip")) from(e.clientX);
  });
  $("#grip").addEventListener("keydown", e => {
    const step = e.shiftKey ? 10 : 1;
    const keys = {ArrowLeft: -step, ArrowRight: step, Home: -S.split, End: 100 - S.split};
    if (e.key in keys) { setSplit(S.split + keys[e.key]); e.preventDefault(); }
  });
})();

/* ---------------------------------------------------------- 全屏对比 */
const STAGE = document.querySelector(".stagebox");
const LOUPE = $("#loupe"), LOUPE_HOME = $("#loupeSlot");

/* 全屏时把放大镜整块挪进光台右侧那一栏。复用的还是同一套三格 ——
   不另建一套 img，也就不会为了「右边也显示」再打一次取块请求。
   退出全屏放回原位。 */
function moveLoupe(intoStage) {
  const target = intoStage ? STAGE : LOUPE_HOME;
  if (LOUPE.parentElement !== target) target.appendChild(LOUPE);
}

function refreshFs() {
  const on = document.fullscreenElement === STAGE;
  moveLoupe(on);
  $("#fsText").textContent = on ? "退出全屏" : "全屏对比";
  $("#fs").setAttribute("aria-pressed", on ? "true" : "false");
  if (S.result) {
    updateLoupe();
    // 挪完 DOM 再量一次：全屏那一栏的格子尺寸跟页面上不一样，
    // 不重算的话 --tilew 会沿用旧值，图就错位了。
    // 倍率标签和滑块读数也得重算 —— 最远那档被短边六成压着，格子一换宽度，
    // 能放到的倍率和滑块两端的位置就都变了。
    requestAnimationFrame(() => {
      if (!S.result) return;
      renderSpan();
      renderZoom();
      updateLoupe();
    });
  }
}
$("#fs").onclick = () => {
  if ($("#fs").getAttribute("aria-disabled") === "true") return;
  try {
    if (document.fullscreenElement) document.exitFullscreen();
    else if (STAGE.requestFullscreen) STAGE.requestFullscreen();
  } catch (e) {
    fail("这台浏览器不给全屏。", "可以直接把窗口拉大，或者用 F 键再试一次。");
  }
};
document.addEventListener("fullscreenchange", refreshFs);

/* 全屏里那颗退出键（只在全屏显示，见 app.css 的 .fsExit）。
   直接调 exitFullscreen，不绕 #fs.click() —— 省得依赖它此刻的 aria-disabled。 */
$("#fsExit").onclick = () => { if (document.fullscreenElement) document.exitFullscreen(); };
window.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (/^(INPUT|SELECT|TEXTAREA)$/.test((document.activeElement || {}).tagName || "")) return;
  if (e.key === "f" || e.key === "F") {
    if ($("#fs").getAttribute("aria-disabled") === "true") return;
    e.preventDefault();
    $("#fs").click();
    return;
  }
  // 全屏下光台铺满，用方向键微调分割线最顺手
  if (!document.fullscreenElement) return;
  if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    if (document.activeElement === $("#grip")) return;   // 别跟 slider 自己那套重复
    setSplit(S.split + (e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 10 : 1));
    e.preventDefault();
  }
});

/* ---------------------------------------------------------- 计量表 */
const row = (label, value, note, dim) =>
  '<div class="lrow' + (dim ? " dim" : "") + '"><dt>' + label + "</dt><dd>" + value + "</dd>" +
  (note ? "<p>" + note + "</p>" : "") + "</div>";
const group = (title, rows) => '<div class="lgroup"><h3>' + title + "</h3>" + rows + "</div>";
const U = s => '<span class="u">' + s + "</span>";
const CN = n => n.toLocaleString("zh-CN");
/* 数值真在，才格式化它。整理时捡回来的旧记录本来就缺项 —— 缺就显示「—」，
   不编一个看着像真的数出来。 */
const num = (v, f) => (typeof v === "number" && isFinite(v) ? f(v) : "—");
const dim2 = j => (j.in_w != null && j.in_h != null) ? j.in_w + " × " + j.in_h : "—";

function renderLedger(j) {
  const size =
    row("输入", dim2(j) + U(j.in_mp != null ? " · " + j.in_mp.toFixed(2) + " MP · " + fmtBytes(j.in_bytes)
                            : (j.in_bytes != null ? " · " + fmtBytes(j.in_bytes) : ""))) +
    row("输出", (j.out_w != null ? CN(j.out_w) + " × " + CN(j.out_h) : "—") +
        U(j.mp != null ? " · " + j.mp.toFixed(2) + " MP · " + fmtBytes(j.out_bytes) : "")) +
    row("倍率", (j.scale != null ? "×" + j.scale : "—") + U(" · 实测 ×" + num(j.scale_actual, v => v)),
        j.passes > 1
          ? "网络原生 ×" + j.net_scale + "，所以串了 " + j.passes + " 趟；差最后那一点用 Lanczos 补齐到正好 ×" + j.scale + "。"
          : (j.passes === 1 ? "一趟网络就直接到位，没有额外插值。" : ""));

  const run =
    row("模型", (j.preset_label || "—") + U(" · 降噪 " + (j.denoise ? dnLabel(j.denoise) : "—"))) +
    row("清晰度", clearLabel(j.clear || "normal") +
        U(j.sharp_amount ? " · 收尾锐化 半径 " + j.sharp_radius + " px / 增益 " + j.sharp_amount : ""),
        j.sharp_amount ? "网络出来的边是渐变过渡，这一步只把边缘收回来；平坦区不动，所以不会磨出噪点。" : "这一档没做任何锐化，输出就是网络的原始结果。") +
    row("明暗", brightLabel(j.bright || "off") +
        U(j.bright_factor && j.bright_factor !== 1 ? " · 整体 ×" + j.bright_factor : ""),
        j.bright_factor && j.bright_factor !== 1
          ? "这一步会让输出比原图亮一点 —— 换的是线上那种通透观感，不是更还原。想最贴原图就切回「原样」。"
          : "没动明暗，输出跟原图一个亮度。") +
    row("后端", (j.device || "—") + U(" · 分块 " + num(j.tile, v => v) + " px")) +
    row("权重", String(j.model || "").replace(/\.onnx$/, "") || "—", null, true);

  const time =
    row("总耗时", num(j.elapsed, v => v.toFixed(1)) + U(" s")) +
    row("其中推理", num(j.t_net, v => v.toFixed(1)) + U(" s")) +
    row("吞吐", num(j.mps, v => v.toFixed(2)) + U(" MP/s"),
        j.mps != null ? "输出像素除以纯推理时间。这一台是 " + j.device + "，所以这个数是它的真实速度。" : "");

  const quality =
    row("回环一致", num(j.rt_ssim, v => "SSIM " + v) +
        U(j.rt_psnr != null ? " · PSNR " + j.rt_psnr + " dB" : ""),
        j.rt_ssim != null ? "把结果缩回原尺寸再跟原图比。越接近 1，说明内容越没被改跑；掉得厉害就是模型在编东西。" : "") +
    row("锐度增益", (j.sharp_gain == null ? "—" : "×" + j.sharp_gain) + U(" · 对照双三次"),
        j.sharp_gain != null
          ? "同一块 512×512 区域内拉普拉斯响应方差之比。原图 " + num(j.sharp_out, v => v) +
            "，双三次 " + num(j.sharp_bicubic, v => v) + "。"
          : "") +
    row("体积", fmtBytes(j.out_bytes) +
        U(j.bicubic_bytes != null ? " · 双三次 " + fmtBytes(j.bicubic_bytes) : " · 双三次没算"),
        j.bicubic_bytes != null
          ? "同为 PNG 无损编码。多出来的那些体积，就是模型补出来的细节和它带出来的噪声。"
          : (j.mp != null
              ? "输出 " + j.mp.toFixed(0) + " MP，整幅双三次 PNG 得现编码一遍才知道体积，代价太大就跳过了 ——" +
                "放大镜中间那格是当场按需算的，跟这个无关，照看。"
              : ""));

  // 整理输出目录时捡回来的旧结果没有日志，上面那些「—」得先解释一句，不然像坏了
  const head = j.recovered
    ? '<p class="hint" style="padding-block-end:10px">这是整理输出目录时捡回来的旧结果 —— ' +
      "当年的日志没留下，耗时与锐度这类指标无从补算，就显示「—」；尺寸、体积是从文件本身读出来的。</p>"
    : "";
  $("#ledger").innerHTML = head + group("尺寸", size) + group("运行", run) +
                           group("时间", time) + group("质量", quality);
}

/* ---------------------------------------------------------- 启动 */
function restoreJob(id) {
  fetch("/api/job/" + id)
    .then(r => (r.ok ? r.json() : null))
    .then(j => {
      if (!j || j.state !== "done") return;
      S.jobId = j.id;
      $("#jobId").textContent = j.id;
      // 把控制台拉回这条任务当时的选择。不这么做的话，左边还停在默认的 4×，
      // 读数写着 3,372 × 5,056，右边挂的却是 6,744 × 10,112 —— 看着像出了 bug。
      if (S.cfg) {
        if (S.cfg.scales.includes(j.scale)) S.sel.scale = j.scale;
        if (S.cfg.presets.some(p => p.id === j.preset)) S.sel.preset = j.preset;
        if (j.denoise) S.sel.denoise = j.denoise;
        if (j.clear && (S.cfg.clear || []).some(x => x.id === j.clear)) S.sel.clear = j.clear;
        if (j.bright && (S.cfg.bright || []).some(x => x.id === j.bright)) S.sel.bright = j.bright;
        if (S.cfg.tiles.includes(j.tile)) S.sel.tile = j.tile;
        renderPresets(S.cfg);
        renderScales(S.cfg);
        renderDenoise(S.cfg);
        renderClear(S.cfg);
        renderBright(S.cfg);
        $("#tile").value = S.sel.tile;
      }
      setSource({url: "/api/job/" + j.id + "/input", name: j.name, w: j.in_w, h: j.in_h});
      S.result = j;
      showResult(j);
    })
    .catch(() => {});
}

(function boot() {
  const q = new URLSearchParams(location.search);
  const theme = q.get("theme");
  applyTheme(theme === "dark" || theme === "light" ? theme
             : (localStorage.getItem("bigpixels.theme") || "system"));
  applyTouchCopy();
  renderZoom();
  fetch("/api/presets")
    .then(r => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(c => {
      S.cfg = c;
      renderEngine(c);
      renderModelWarn(c);
      renderPresets(c);
      renderScales(c);
      renderDenoise(c);
      renderClear(c);
      renderBright(c);
      renderTiles(c);
      renderSamples(c);
      renderKeep((c.settings || {}).keep !== false);
      applyPresetRules();
      const job = q.get("job"), demo = q.get("demo");
      if (job) return restoreJob(job);
      if (demo) {
        const i = (c.samples || []).findIndex(s => s.id === demo);
        const btn = $("#sampleRow").children[i];
        if (btn) btn.click();
      }
    })
    .catch(e => {
      $("#engine").innerHTML = '<span class="chip"><i class="dot warn"></i>引擎没应答</span>';
      fail("连不上本地服务。", "确认 web/server.py 还在跑，然后刷新这一页。" + String(e.message || e));
    });
})();
