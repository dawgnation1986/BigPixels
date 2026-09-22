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

# 让 app.core.i18n 可导入：它只用标准库，依赖还没装也能跑（见 app/core/__init__.py）
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from app.core.i18n import t, set_lang


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
        print(t("boot.spawn", cmd=cmd[0], err=e))
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
        description=t("env.desc"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=t("arg.exitcode.env"))
    ap.add_argument("--venv", default=DEFAULT_VENV, help=t("arg.venv"))
    ap.add_argument("--requirements", default=DEFAULT_REQ, help=t("arg.req"))
    ap.add_argument("--no-deps", action="store_true", help=t("arg.no_deps"))
    ap.add_argument("--check", action="store_true", help=t("arg.check"))
    ap.add_argument("--lang", choices=("zh", "en", "ja"), default=None,
                    help=t("lang.arghelp"))
    a = ap.parse_args()

    if a.lang:
        set_lang(a.lang)

    venv = os.path.abspath(a.venv)
    py = venv_python(venv)

    print(t("env.venv", venv=venv))
    print(t("env.python", py=py))

    # ---------------------------------------------------------------- 已经好了
    if os.path.isfile(py):
        ok, info = probe(py)
        if ok and not a.no_deps:
            print(t("env.state.ok"))
            for line in info.splitlines():
                print("            " + line)
            print()
            return 0
        if ok:
            print(t("env.state.nodeps"))
            print()
            return 0
        if a.check:
            print(t("env.state.incomplete", info=info))
            print("\n" + t("env.check.install") + "\n")
            return 1
        print(t("env.state.incomplete.go", info=info))
    else:
        if a.check:
            print(t("env.state.novenv"))
            print("\n" + t("env.check.create") + "\n")
            return 1
        # ------------------------------------------------------------ 建虚拟环境
        print()
        print(t("env.build", ver=sys.version.split()[0]))
        if run([sys.executable, "-m", "venv", venv]):
            print()
            print(t("env.build.fail"))
            print(t("env.build.fail.win"))
            print(t("env.build.fail.linux"))
            return 1
        if not os.path.isfile(py):
            print(t("env.build.nopy", py=py))
            return 1
        print(t("env.build.ok"))

    if a.no_deps:
        print("\n" + t("env.nodeps") + "\n")
        return 0

    # ---------------------------------------------------------------- 装依赖
    req = os.path.abspath(a.requirements)
    if not os.path.isfile(req):
        print(t("env.req.missing", req=req))
        return 2
    print()
    print(t("env.req.install", name=os.path.basename(req)))
    print(t("env.req.note1"))
    print(t("env.req.note2"))
    print()
    run([py, "-m", "pip", "install", "--upgrade", "pip"], quiet=True)
    if run([py, "-m", "pip", "install", "-r", req]):
        print()
        print(t("env.req.retry"))
        if run([py, "-m", "pip", "install", "-r", req, "-i", MIRROR]):
            print()
            print(t("env.req.fail"))
            print(t("env.req.fail.net"))
            print(t("env.req.fail.brk"))
            return 1

    # ---------------------------------------------------------------- 收尾复核
    ok, info = probe(py)
    if not ok:
        print("\n" + t("env.import.fail", info=info))
        print(t("env.import.hint"))
        return 1
    print("\n" + t("env.done"))
    for line in info.splitlines():
        print("            " + line)
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n" + t("env.interrupt") + "\n")
        sys.exit(1)
