#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用户设置，存在 outputs/settings.json。

写设置的地方只有这里一处 —— 网页服务、启动脚本、语言模块都调它，
免得两边各写一份、互相覆盖。
"""
from __future__ import annotations

import json
import os

from .paths import OUTPUT_DIR, SETTINGS_PATH

# 键 -> 默认值。类型以默认值为准校验；写回时按这个顺序落盘。
DEF: dict = {
    "keep": True,      # True 保存模式（默认）/ False 暂存模式
    "lang": "zh",      # 界面语言：zh / en / ja
}


def stored() -> dict:
    """盘上真有的那一份，不补默认值。

    用来分辨「用户到底选过没有」—— load() 会把默认值补齐，于是
    「从没选过语言」和「选了默认语言」在它眼里是同一个样子。
    """
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load() -> dict:
    cfg = dict(DEF)
    raw = stored()
    for k, dv in DEF.items():
        v = raw.get(k)
        if isinstance(dv, bool):
            if isinstance(v, bool):
                cfg[k] = v
        elif isinstance(dv, str):
            if isinstance(v, str) and v:
                cfg[k] = v
    return cfg


def save(cfg: dict) -> None:
    """整份落盘。先写临时文件再替换，中途出错不会留下半个文件。"""
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: cfg.get(k, dv) for k, dv in DEF.items()},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except OSError:
        pass
