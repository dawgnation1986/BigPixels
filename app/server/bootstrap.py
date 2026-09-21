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
        print(f"  起不了进程 {cmd[0]}：{e}")
        return 1


def step(n: int, title: str) -> None:
    print(f"\n  [{n}/{TOTAL_STEPS}] {title}")
    sys.stdout.flush()


def hold(msg: str = "\n  按回车关闭这个窗口…") -> None:
    """出错时把窗口停住。

    双击启动的窗口跑完就关，用户根本来不及看上面写了什么，所以失败必须停住。
    但手动在终端里跑的时候停住就很烦，所以只在「真的对着一个控制台」时才等。
    """
    if NO_PAUSE or not sys.stdin.isatty():
        return
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass


NO_PAUSE = False


# --------------------------------------------------------------------------- #
def banner() -> None:
    print()
    print("  BigPixels 放大台 · 本地版")
    print("  在本机运行的 AI 无损放大；图片不会离开本机，也不会上传至任何位置。")
    print("  " + "-" * 62)


def main() -> int:
    global NO_PAUSE

    ap = argparse.ArgumentParser(
        description="BigPixels 一键启动：环境 → 模型 → 网页服务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0 成功 / 1 中间步骤失败 / 2 参数错误")
    ap.add_argument("--port", type=int, default=8765, help="网页服务端口（默认 8765）")
    ap.add_argument("--venv", default=DEFAULT_VENV, help="虚拟环境目录（默认 .venv）")
    ap.add_argument("--source", choices=("mirror", "hf"), default="mirror",
                    help="模型下载源：mirror 国内镜像（默认）/ hf 原站")
    ap.add_argument("--check", action="store_true",
                    help="只自检环境和模型，不安装、不下载、不起服务")
    ap.add_argument("--no-open", action="store_true", help="起服务但不自动开浏览器")
    ap.add_argument("--no-pause", action="store_true", help="出错时不停留等待回车")
    a = ap.parse_args()

    NO_PAUSE = a.no_pause

    # 自己这一句也别因为编码问题崩掉
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")       # type: ignore[union-attr]
        except Exception:
            pass

    banner()
    venv = os.path.abspath(a.venv)
    py = venv_python(venv)

    # ---------------------------------------------------------------- 1/3 环境
    # .venv 还没建的时候手上只有系统 python，得用它去建。setup_env.py 自己会
    # 判断「建环境 / 补依赖 / 已经好了」，所以这里只要把解释器选对就行。
    have_venv = os.path.isfile(py)
    step(1, "运行环境" + ("（首次运行，先准备虚拟环境与依赖）" if not have_venv else ""))
    cmd = [py if have_venv else sys.executable, SETUP, "--venv", venv]
    if a.check:
        cmd.append("--check")
    rc = run(cmd)
    if rc:
        print("\n  ！运行环境未就绪 —— 具体失败步骤见上方输出。")
        print("     Windows 上多为未安装 Python 或安装不完整：请到 python.org 安装")
        print("     3.9 以上版本（安装时勾选 \"Add python.exe to PATH\"），然后重新双击启动。")
        hold()
        return 1
    py = venv_python(venv)
    if not os.path.isfile(py):
        print(f"\n  ！环境显示已建好，却找不到 {py}")
        hold()
        return 1

    # ---------------------------------------------------------------- 2/3 模型
    step(2, "模型自检（15 个权重 · 约 292 MB · 缺什么下什么）")
    if run([py, MODELS, "--source", a.source]):
        # download_models.py 自己已经把「重跑续传 / 换源 / 手动放文件」说清了
        print("\n  ！模型未下载完整。缺失的模型在网页中不可用，其余功能不受影响。")
        print("     已下载的文件不会重复下载，重新运行即从断点续传。")
        hold()
        return 1

    # ---------------------------------------------------------------- 3/3 服务
    if a.check:
        step(3, "网页服务")
        print("        --check：已跳过。以上两步均无问题，去掉 --check 即可启动服务。\n")
        return 0

    url = f"http://127.0.0.1:{a.port}/"
    step(3, "起网页服务")
    print(f"        地址   {url}")
    print("        停止   在本窗口按 Ctrl+C，或直接关闭本窗口")
    print()

    cmd = [py, SERVER, "--port", str(a.port)]
    if not a.no_open:
        cmd.append("--open")
    rc = run(cmd)

    print()
    if rc:
        print(f"  ！服务已退出（退出码 {rc}）。")
        print(f"     若上方提示「端口 {a.port} 无法启动」，说明该端口已被占用 ——")
        print("     通常是上次的窗口仍在运行。请关闭它，或改用其他端口：")
        print(f"         python app\\server\\bootstrap.py --port {a.port + 1}")
        hold()
        return 1
    print("  服务已停止。\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  已手动中断。已下载的模型与已安装的依赖均保留，重新运行将从中断处继续。\n")
        sys.exit(0)
