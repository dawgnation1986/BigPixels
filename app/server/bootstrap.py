#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BigPixels 放大台 · 一键启动

双击 start_web.bat / start_web.sh 跑的就是它。单独跑也行：

    python app/server/bootstrap.py                # 环境 → 模型 → 起服务并开浏览器
    python app/server/bootstrap.py --check        # 只自检（环境 + 模型），不装不下不起服务
    python app/server/bootstrap.py --no-open      # 起服务但不自动开浏览器
    python app/server/bootstrap.py --port 8766    # 换端口（默认 8765）
    python app/server/bootstrap.py --source hf    # 模型走 huggingface.co（默认走国内 hf-mirror）

退出码：0 跑通了 / 1 中间某步没成 / 2 参数不对

------------------------------------------------------------------------------
这一层为什么存在

双击启动的 .bat 必须保持**纯 ASCII**：cmd 读 .bat 是按字节偏移一行行读的，
文件里只要出现一个多字节字符，它的「字符数」和「字节数」就对不上，行会读串
（实测报出 'd'、'\\pip' 这种碎片，然后后面一半命令干脆没执行）。
所以 .bat 里一个字的中文都不能留 —— 中文提示全部归 python。

既然提示都在 python 了，就得有个地方把三步串起来、统一收尾（失败停住等回车、
端口被占怎么说），于是有了这个文件。.bat 和 .sh 因此都缩成二十来行的壳。

三步各自还有独立的入口，单独调也行：
    1/3  app/server/setup_env.py          虚拟环境 + 依赖
    2/3  app/tools/download_models.py     15 个权重，缺什么下什么
    3/3  app/server/server.py             网页服务
------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))      # app/server -> app -> 项目根
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 词条与设置只用标准库，所以「依赖还没装」的第一次运行也能用它们 ——
# app/core 下面那几个吃 numpy 的模块是懒加载的，这一句 import 不会碰到它们。
import app.core as U                                # noqa: E402

SETUP = os.path.join(HERE, "setup_env.py")
MODELS = os.path.join(ROOT, "tools", "download_models.py")   # 入口薄壳在 tools/ 下
SERVER = os.path.join(HERE, "server.py")
DEFAULT_VENV = os.path.join(ROOT, ".venv")

TOTAL_STEPS = 3


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def venv_python(venv: str) -> str:
    """虚拟环境里的解释器（Windows 和 POSIX 各一个位置）"""
    if os.name == "nt":
        return os.path.join(venv, "Scripts", "python.exe")
    return os.path.join(venv, "bin", "python")


def child_env() -> dict:
    """子进程一律按 UTF-8 说话。

    启动脚本已经把控制台切到 65001（chcp）了，这里再钉一遍 ——
    .sh 那边或者手动调的时候就没有 chcp，不钉的话中文会按系统编码出去变乱码。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run(cmd: list[str]) -> int:
    """跑个子进程并把它的输出直接透出来（pip 和下载进度得让用户看见）"""
    # 先把我们自己的缓冲吐干净再让子进程接管 —— 否则输出被重定向 / 走管道时
    # 两边各缓各的，子进程的话会插到提示前面去，看着像顺序错乱。
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        return subprocess.call(cmd, env=child_env())
    except OSError as e:
        print("  " + U.t("boot.spawn", cmd=cmd[0], err=e))
        return 1


def step(n: int, title: str) -> None:
    print(f"\n  [{n}/{TOTAL_STEPS}] {title}")
    sys.stdout.flush()


def hold(msg: str | None = None) -> None:
    """出错时把窗口停住。

    双击启动的窗口跑完就关，用户根本来不及看上面写了什么，所以失败必须停住。
    但手动在终端里跑的时候停住就很烦，所以只在「真的对着一个控制台」时才等。
    """
    if NO_PAUSE or not sys.stdin.isatty():
        return
    try:
        input(msg if msg is not None else U.t("boot.pause"))
    except (EOFError, KeyboardInterrupt):
        pass


NO_PAUSE = False


def ask_lang() -> str | None:
    """第一次运行时问一次界面语言。

    只在「对着一个控制台」时问：没有终端（脚本、CI、重定向）就一律沿用默认，
    免得整条启动流程卡在一个没人能回答的提问上。
    答什么都不认识时按默认走 —— 宁可语言猜错一次（网页里随时能改），
    也别让人因为手滑而被挡在启动之外。
    """
    if not sys.stdin.isatty():
        return None
    print()
    print("  " + U.t("lang.title"))
    for i, code in enumerate(U.LANGS, 1):
        print(f"    {i}) {U.LANG_NAMES[code]}")
    try:
        raw = input("  " + U.t("lang.hint") + " ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    pick = {"": U.LANGS[0], "1": "zh", "2": "en", "3": "ja"}.get(raw, U.LANGS[0])
    return pick


# --------------------------------------------------------------------------- #
def banner() -> None:
    print()
    print("  " + U.t("app.title"))
    print("  " + U.t("app.tagline"))
    print("  " + "-" * 62)


def main() -> int:
    global NO_PAUSE

    ap = argparse.ArgumentParser(
        description=U.t("boot.desc"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=U.t("arg.exitcode"))
    ap.add_argument("--port", type=int, default=8765, help=U.t("arg.port"))
    ap.add_argument("--venv", default=DEFAULT_VENV, help=U.t("arg.venv"))
    ap.add_argument("--source", choices=("mirror", "hf"), default="mirror",
                    help=U.t("arg.source"))
    ap.add_argument("--lang", choices=list(U.LANGS), default=None,
                    help=U.t("lang.arghelp"))
    ap.add_argument("--check", action="store_true", help=U.t("arg.check"))
    ap.add_argument("--no-open", action="store_true", help=U.t("arg.no_open"))
    ap.add_argument("--no-pause", action="store_true", help=U.t("arg.no_pause"))
    a = ap.parse_args()

    NO_PAUSE = a.no_pause

    # 自己这一句也别因为编码问题崩掉
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")       # type: ignore[union-attr]
        except Exception:
            pass

    # 语言：命令行给了就用它；没给、又从来没选过，就问一次。
    # 问完立刻落盘，所以「第一次运行」只会出现一次。
    if a.lang:
        U.i18n.set_lang(a.lang)
    elif U.i18n.chosen() is None:
        picked = ask_lang()
        if picked:
            U.i18n.set_lang(picked)
            print("  " + U.t("lang.saved", name=U.LANG_NAMES[picked]))

    banner()
    venv = os.path.abspath(a.venv)
    py = venv_python(venv)
    lang_arg = ["--lang", U.lang_of()]

    # ---------------------------------------------------------------- 1/3 环境
    # .venv 还没建的时候手上只有系统 python，得用它去建。setup_env.py 自己会
    # 判断「建环境 / 补依赖 / 已经好了」，所以这里只要把解释器选对就行。
    have_venv = os.path.isfile(py)
    step(1, U.t("step.env") + (U.t("step.env.first") if not have_venv else ""))
    cmd = [py if have_venv else sys.executable, SETUP, "--venv", venv] + lang_arg
    if a.check:
        cmd.append("--check")
    rc = run(cmd)
    if rc:
        print("\n  " + U.t("env.runfail"))
        print("     " + U.t("env.runfail.win1"))
        print("     " + U.t("env.runfail.win2"))
        hold()
        return 1
    py = venv_python(venv)
    if not os.path.isfile(py):
        print("\n  " + U.t("env.runfail.nopy", py=py))
        hold()
        return 1

    # ---------------------------------------------------------------- 2/3 模型
    step(2, U.t("step.models"))
    if run([py, MODELS, "--source", a.source] + lang_arg):
        # download_models.py 自己已经把「重跑续传 / 换源 / 手动放文件」说清了
        print("\n  " + U.t("env.modelsfail"))
        print("     " + U.t("env.modelsfail.resume"))
        hold()
        return 1

    # ---------------------------------------------------------------- 3/3 服务
    if a.check:
        step(3, U.t("step.server"))
        print("        " + U.t("env.check.skip") + "\n")
        return 0

    url = f"http://127.0.0.1:{a.port}/"
    step(3, U.t("step.server.start"))
    print("        " + U.t("env.server.addr", url=url))
    print("        " + U.t("env.server.stop"))
    print()

    cmd = [py, SERVER, "--port", str(a.port)] + lang_arg
    if not a.no_open:
        cmd.append("--open")
    rc = run(cmd)

    print()
    if rc:
        print("  " + U.t("env.server.exit", rc=rc))
        print("     " + U.t("env.server.busy", port=a.port))
        print("     " + U.t("env.server.busy2"))
        print(f"         python app\\server\\bootstrap.py --port {a.port + 1}")
        hold()
        return 1
    print("  " + U.t("env.server.stopped") + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  " + U.t("env.interrupt2") + "\n")
        sys.exit(0)
