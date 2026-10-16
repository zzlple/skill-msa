# -*- coding: utf-8 -*-
"""msa_env.py —— 运行容器（客户端）动态识别

面板标题（浏览器选项卡 + 页面标题行）需要体现「当前是哪个客户端容器在执行本 skill」，
所以这里不写死名称，而是运行期多路探测：

    1) 显式环境变量 MSA_CLIENT_NAME —— 人工覆盖，最高优先级
    2) 容器注入的品牌环境变量前缀（MARVIS_* / WORKBUDDY_* / DOUBAO_* …）
    3) 进程祖先链 —— 谁拉起了当前解释器（Marvis / WorkBuddy / Doubao …）
    4) skill 所在目录特征（.marvis / .workbuddy / Doubao）
    5) 兜底默认 "Marvis 电脑端"

对外只暴露一个函数：detect_container()
"""
from __future__ import annotations

import os

DEFAULT_CLIENT = "Marvis 电脑端"

# 关键词 -> 客户端显示名（小写匹配）
_RULES = [
    ("workbuddy", "WorkBuddy"),
    ("codebuddy", "WorkBuddy"),
    ("doubao", "豆包"),
    ("豆包", "豆包"),
    ("marvis", "Marvis 电脑端"),
]


def _match(text):
    t = (text or "").lower()
    for key, name in _RULES:
        if key in t:
            return name
    return None


# --------------------------------------------------------------------------
# 1) 环境变量
# --------------------------------------------------------------------------
def _explicit_container():
    return (os.environ.get("MSA_CLIENT_NAME") or "").strip() or None


def _env_container():
    """按规则优先级扫描容器注入的品牌环境变量前缀。"""
    for key, name in _RULES:
        if not key.isascii():
            continue
        prefix = key.upper()
        for k in os.environ:
            if k.upper().startswith(prefix):
                return name
    return None


# --------------------------------------------------------------------------
# 2) 进程祖先链（纯标准库，ctypes 调 Toolhelp32，无第三方依赖）
# --------------------------------------------------------------------------
def _process_table():
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return {}
    table = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return table


def _proc_container(max_depth=12):
    if os.name != "nt":
        return None
    try:
        table = _process_table()
    except Exception:
        return None
    if not table:
        return None
    pid = os.getpid()
    seen = set()
    for _ in range(max_depth):
        if pid in seen or pid not in table:
            break
        seen.add(pid)
        ppid, exe = table[pid]
        hit = _match(exe)
        if hit:
            return hit
        pid = ppid
    return None


# --------------------------------------------------------------------------
# 3) skill 所在目录特征
# --------------------------------------------------------------------------
def _path_container():
    try:
        skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        return None
    return _match(skill_root)


def detect_container():
    """返回当前执行容器的显示名，探测不到时返回 DEFAULT_CLIENT。

    优先级：显式环境变量 > 进程祖先链（最真实）> 容器品牌环境变量 > skill 目录特征。
    """
    for probe in (_explicit_container, _proc_container, _env_container, _path_container):
        try:
            name = probe()
        except Exception:
            name = None
        if name:
            return name
    return DEFAULT_CLIENT


if __name__ == "__main__":
    print(detect_container())
