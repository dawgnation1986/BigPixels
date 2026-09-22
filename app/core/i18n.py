#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""界面语言（zh / en / ja）。

界面上给人看的每一句话都在 app/locales/<lang>.json 里，代码里不写死文案 ——
所以多一种语言 = 多一个 json 文件，不用改逻辑。

    首次运行   app/server/bootstrap.py 问一次，存进 outputs/settings.json
    之后改     网页「更多设置」里切换，或命令行 --lang

只依赖标准库：bootstrap / setup_env 要在「依赖还没装」的第一次运行里就用上它。
"""
from __future__ import annotations

import json
import os

from . import settings as _settings
from .paths import LOCALE_DIR

LANGS = ("zh", "en", "ja")
DEFAULT = "zh"
# 选语言时显示的名字用各语言自己的写法 —— 挑语言的人还不一定会读出英文的「Japanese」。
NAMES = {"zh": "简体中文", "en": "English", "ja": "日本語"}

_tables: dict[str, dict] = {}
_current: str | None = None


def valid(code) -> bool:
    return isinstance(code, str) and code in LANGS


def current() -> str:
    """当前语言。进程内缓存一次 —— 中途要靠 set_lang() 改。"""
    global _current
    if _current is None:
        code = _settings.load().get("lang")
        _current = code if valid(code) else DEFAULT
    return _current


def chosen() -> str | None:
    """用户自己选过的语言；从没选过就是 None。

    启动时靠它决定「要不要问一次」—— current() 会把默认值算进去，
    分不出「没选过」和「选了默认那个」。
    """
    code = _settings.stored().get("lang")
    return code if valid(code) else None


def set_lang(code: str) -> None:
    """改语言并落盘（保留设置里的其它项）。"""
    global _current
    if not valid(code):
        raise ValueError(f"unknown language: {code!r}")
    cfg = _settings.load()
    cfg["lang"] = code
    _settings.save(cfg)
    _current = code


def table(code: str | None = None) -> dict:
    """某一语言的整份词条表。读不到就给空表，界面上会退回中文。"""
    code = code or current()
    if code not in _tables:
        path = os.path.join(LOCALE_DIR, f"{code}.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            _tables[code] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _tables[code] = {}
    return _tables[code]


def t(key: str, **kw) -> str:
    """取一条词条。

    当前语言缺这条就退回中文，中文也没有就把键名原样返回 ——
    宁可界面上冒出一个 `ui.foo`，也别让整个流程因为少一句说明而中断。
    """
    s = table().get(key)
    if s is None and current() != DEFAULT:
        s = table(DEFAULT).get(key)
    if s is None:
        return key
    return s.format(**kw) if kw else s


def web_table(code: str | None = None) -> dict:
    """给前端的词条（ui.* 那一段），由 /api/presets 一并送出去。"""
    return {k: v for k, v in table(code).items() if k.startswith("ui.")}
