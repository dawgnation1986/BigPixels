#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一个够用的 Chrome DevTools Protocol 客户端，只用标准库。

为什么不用现成的：这个环境里没有 websocket 客户端，而为了「拍一张全屏版的图」
「量一下鼠标拖动到底掉不掉帧」去装一个依赖不划算 —— CDP 的线协议就那么点东西。

    from cdp import Session
    with Session("http://127.0.0.1:8765/", size=(1440, 1180)) as s:
        s.wait_ready()
        s.shot("out.png")
        s.js("document.querySelector('.stagebox').requestFullscreen()", gesture=True)
        s.shot("full.png")

不写 C 盘：user-data-dir 由调用方指到工作区里。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser() -> str:
    for p in BROWSERS:
        if os.path.isfile(p):
            return p
    raise SystemExit("找不到 Chrome / Edge，装一个再来")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ WebSocket -- #
class WS:
    """客户端 WebSocket，刚够说 CDP。"""

    def __init__(self, url: str, timeout: float = 30.0):
        assert url.startswith("ws://"), url
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        head = (f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(head.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("握手时连接被关掉了")
            buf += chunk
        status, _, rest2 = buf.partition(b"\r\n")
        if b" 101 " not in status:
            raise IOError("WebSocket 握手失败：" + status.decode("latin-1")[:120])
        self.buf = rest2.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in rest2 else b""

    def _exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise IOError("连接断了")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        data = text.encode("utf-8")
        n = len(data)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        self.sock.sendall(bytes(hdr) + masked)

    def recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._exact(2)
            fin, op, ln = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._exact(8))[0]
            payload = self._exact(ln)
            if op == 0x9:                       # ping -> pong
                self.sock.sendall(b"\x8a\x80" + b"\x00\x00\x00\x00")
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise IOError("服务端关掉了连接")
            parts.append(payload)
            if fin:
                break
        return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------------ 会话 -- #
class Session:
    def __init__(self, url: str, size=(1440, 1180), profile: str | None = None,
                 headless: bool = True):
        self.port = _free_port()
        self.profile = profile or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", ".cache", "cdp-profile")
        self.profile = os.path.abspath(self.profile)
        os.makedirs(self.profile, exist_ok=True)
        args = [find_browser(), "--user-data-dir=" + self.profile,
                "--remote-debugging-port=" + str(self.port),
                "--no-first-run", "--no-default-browser-check",
                "--hide-scrollbars", "--force-device-scale-factor=1",
                "--disable-features=Translate", "--no-service-autorun",
                "--window-size=%d,%d" % size]
        if headless:
            args.insert(1, "--headless=new")
            args.insert(2, "--disable-gpu")
        args.append(url)
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.ws = None
        self._id = 0
        self.events: list[dict] = []
        self._connect()

    def _connect(self, timeout: float = 30.0):
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/list", timeout=2) as r:
                    tabs = json.loads(r.read().decode("utf-8"))
                page = next((t for t in tabs if t.get("type") == "page"
                             and t.get("webSocketDebuggerUrl")), None)
                if page:
                    self.ws = WS(page["webSocketDebuggerUrl"])
                    return
            except Exception as e:          # noqa: BLE001 —— 还没起来，继续等
                last = e
            time.sleep(0.25)
        raise SystemExit(f"连不上 Chrome 的调试端口 {self.port}：{last}")

    def call(self, method: str, timeout: float = 60.0, **params):
        self._id += 1
        mid = self._id
        self.ws.sock.settimeout(timeout)
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败：{msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def js(self, expr: str, gesture: bool = False, wait: bool = False):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=wait, userGesture=gesture)
        if "exceptionDetails" in r:
            raise RuntimeError("JS 报错：" + json.dumps(
                r["exceptionDetails"].get("exception", {}), ensure_ascii=False)[:300])
        return r.get("result", {}).get("value")

    def ready(self, timeout: float = 25.0) -> bool:
        """等到页面把预设渲染出来 —— 这说明 /api/presets 已经回来了。"""
        t0 = time.time()
        expr = ('document.readyState === "complete" && '
                '!!document.querySelector("#presets") && '
                'document.querySelector("#presets").children.length > 0')
        while time.time() - t0 < timeout:
            try:
                if self.js(expr):
                    return True
            except Exception:               # noqa: BLE001 —— 还在导航，下一轮再说
                pass
            time.sleep(0.2)
        return False

    def wait_for(self, expr: str, timeout: float = 60.0, poll: float = 0.25) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.js(expr):
                    return True
            except Exception:               # noqa: BLE001
                pass
            time.sleep(poll)
        return False

    def mouse(self, x: int, y: int, kind: str = "mouseMoved", button: str = "none",
              buttons: int = 0, clicks: int = 0):
        self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y,
                  button=button, buttons=buttons, clickCount=clicks)

    def box(self, selector: str):
        return self.js("(() => {const e = document.querySelector(%s);"
                       "if(!e) return null; const r = e.getBoundingClientRect();"
                       "return {x:r.x, y:r.y, w:r.width, h:r.height};})()"
                       % json.dumps(selector))

    def shot(self, path: str, full_page: bool = False) -> bool:
        r = self.call("Page.captureScreenshot", format="png",
                      captureBeyondViewport=bool(full_page))
        data = base64.b64decode(r.get("data", ""))
        if len(data) < 2000:
            return False
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return True

    def close(self):
        try:
            self.call("Browser.close", timeout=5)
        except Exception:                   # noqa: BLE001
            pass
        if self.ws:
            self.ws.close()
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def __enter__(self):
        self.call("Page.enable")
        self.call("Runtime.enable")
        return self

    def __exit__(self, *exc):
        self.close()
        return False
