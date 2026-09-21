#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BigPixels 放大台 · 把运行环境备好（虚拟环境 + 依赖）

双击启动脚本第一步就会调它。单独跑也行：

    python app/server/setup_env.py                   # 缺什么补什么（已经好了就一句话带过）
    python app/server/setup_env.py --check           # 只检查，不动手
    python app/server/setup_env.py --no-deps         # 只建虚拟环境，不装依赖
    python app/server/setup_env.py --venv .venv-gpu  # 换个环境目录
    python app/server/setup_env.py --requirements req-gpu.txt
                                              # 换一份依赖清单（比如换 onnxruntime-gpu）

退出码：0 好了 / 1 没弄好 / 2 参数或环境有问题

为什么中文提示在这儿、不在 .bat 里：cmd 读 .bat 是按字节偏移一行行读的，
文件里只要有多字节字符，它的"字符数"和"字节数"就对不上，行会读串
（实测会报出 'd'、'\\pip' 这种碎片，然后一半命令干脆没执行）。
所以 .bat 保持纯 ASCII、只管调度，说话的事全部交给 python。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_VENV = os.path.join(ROOT, ".venv")
DEFAULT_REQ = os.path.join(ROOT, "requirements.txt")

# 这三个都能 import 才算环境好了。onnxruntime 只挑一个名字（directml / gpu / cpu 都叫它）
NEED = ("numpy", "PIL", "onnxruntime")

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def venv_python(venv: str) -> str:
    """虚拟环境里的解释器路径（Windows 和 POSIX 各一个位置）"""
    if os.name == "nt":
        return os.path.join(venv, "Scripts", "python.exe")
    return os.path.join(venv, "bin", "python")


def run(cmd: list[str], quiet: bool = False) -> int:
    """跑个子进程，把它的话直接透出来（pip 的进度得让用户看见）"""
    try:
        return subprocess.call(cmd, stdout=subprocess.DEVNULL if quiet else None,
                               stderr=None if quiet else None)
    except OSError as e:
        print(f"     起不了进程 {cmd[0]}：{e}")
        return 1


def probe(py: str) -> tuple[bool, str]:
    """用某个解释器试 import 一次，顺带把版本抓回来。

    比"记一个标记文件说装过了"可靠得多 —— 用户手动删过、换过后端、
    或者装了一半失败，这里都能当场看出来。"""
    code = ("import sys\n"
            "try:\n"
            "    import numpy, PIL, onnxruntime as ort\n"
            "except Exception as e:\n"
            "    print('MISS ' + type(e).__name__)\n"
            "    sys.exit(1)\n"
            "print('OK %s | numpy %s | pillow %s | onnxruntime %s'\n"
            "      % (sys.version.split()[0], numpy.__version__,\n"
            "         PIL.__version__, ort.__version__))\n"
            "try:\n"
            "    print('providers ' + ', '.join(ort.get_available_providers()))\n"
            "except Exception:\n"
            "    pass\n")
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=180)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return r.returncode == 0, (r.stdout or r.stderr or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="BigPixels 运行环境自检 / 安装",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0 好了 / 1 没弄好 / 2 参数或环境有问题")
    ap.add_argument("--venv", default=DEFAULT_VENV, help="虚拟环境目录（默认 .venv）")
    ap.add_argument("--requirements", default=DEFAULT_REQ, help="依赖清单（默认 requirements.txt）")
    ap.add_argument("--no-deps", action="store_true", help="只建虚拟环境，不装依赖")
    ap.add_argument("--check", action="store_true", help="只检查，不创建也不安装")
    a = ap.parse_args()

    venv = os.path.abspath(a.venv)
    py = venv_python(venv)

    print(f"  运行环境  {venv}")
    print(f"  解释器    {py}")

    # ---------------------------------------------------------------- 已经好了
    if os.path.isfile(py):
        ok, info = probe(py)
        if ok and not a.no_deps:
            print("  状态      已经好了，跳过安装")
            for line in info.splitlines():
                print("            " + line)
            print()
            return 0
        if ok:
            print("  状态      环境在，但 --no-deps：不检查依赖")
            print()
            return 0
        if a.check:
            print(f"  状态      依赖不全 → {info}")
            print("\n  去掉 --check 就会把缺的装上。\n")
            return 1
        print(f"  状态      依赖不全（{info}），接着装")
    else:
        if a.check:
            print("  状态      虚拟环境还没建")
            print("\n  去掉 --check 就会建好它。\n")
            return 1
        # ------------------------------------------------------------ 建虚拟环境
        print()
        print(f"  建虚拟环境（用 {sys.version.split()[0]} 建）… 只做这一次")
        if run([sys.executable, "-m", "venv", venv]):
            print()
            print("  ！创建虚拟环境失败。常见原因有两种：")
            print("     · Windows：Python 安装不完整 —— 请到 python.org 安装 3.9 以上版本")
            print("     · Linux：缺少 venv 组件 —— 执行 sudo apt install python3-venv")
            return 1
        if not os.path.isfile(py):
            print(f"  ！创建完成却找不到 {py}，该 Python 的 venv 模块可能有问题")
            return 1
        print("  虚拟环境已就绪")

    if a.no_deps:
        print("\n  --no-deps：仅创建环境，不安装依赖（需要安装时请去掉此参数）\n")
        return 0

    # ---------------------------------------------------------------- 装依赖
    req = os.path.abspath(a.requirements)
    if not os.path.isfile(req):
        print(f"  ！找不到依赖清单 {req}")
        return 2
    print()
    print(f"  安装依赖（{os.path.basename(req)}）—— 约数百 MB，首次安装需要等待一段时间")
    print("  说明：Windows 安装 onnxruntime-directml（可直接调用显卡）")
    print("        其它系统安装 onnxruntime（CPU）。使用 CUDA 的方法见 README。")
    print()
    run([py, "-m", "pip", "install", "--upgrade", "pip"], quiet=True)
    if run([py, "-m", "pip", "install", "-r", req]):
        print()
        print("        默认源安装失败，改用清华镜像重试…")
        if run([py, "-m", "pip", "install", "-r", req, "-i", MIRROR]):
            print()
            print("  ！依赖安装失败。常见原因有两种：")
            print("     · 网络不通 —— 配置代理后重试")
            print("     · 安装中断 —— 直接重新运行启动脚本，pip 会继续安装")
            return 1

    # ---------------------------------------------------------------- 收尾复核
    ok, info = probe(py)
    if not ok:
        print(f"\n  ！安装完成却仍无法 import：{info}")
        print("     通常是后端安装有误 —— 请检查 requirements.txt 中那两行平台标记。")
        return 1
    print("\n  好了：")
    for line in info.splitlines():
        print("            " + line)
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  手动打断了。重跑一次会接着来。\n")
        sys.exit(1)
