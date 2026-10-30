#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进阶工具：联网搜索、抓网页、读写文件、执行代码。

这些工具「威力大」，所以都做了限制：
- 文件操作被关在 sandbox 目录里，出不去。
- 执行代码默认关闭，要用得手动开（ENABLE_CODE_EXEC=1）。
"""

import os
import re
import subprocess
import sys
import urllib.parse

import requests

from agent_core import Tool

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX = os.path.join(HERE, "workspace")          # 文件工具的牢笼
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36"}


# ============================================================ 联网
def web_search(query: str, max_results: int = 5):
    """用 DuckDuckGo 搜，不需要 API key。"""
    try:
        r = requests.post("https://html.duckduckgo.com/html/",
                          data={"q": query}, headers=UA, timeout=15)
        # 结果标题和链接藏在 <a class="result__a" href="...">标题</a>
        items = re.findall(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            r.text, re.S)
        out = []
        for href, title in items[:int(max_results)]:
            title = re.sub(r"<[^>]+>", "", title).strip()
            # DuckDuckGo 的跳转链接里带真实地址，解出来
            if "uddg=" in href:
                href = urllib.parse.unquote(
                    urllib.parse.parse_qs(
                        urllib.parse.urlparse(href).query)["uddg"][0])
            out.append(f"{title}\n    {href}")
        return "\n\n".join(out) if out else f"没搜到「{query}」的结果。"
    except Exception as e:
        return f"搜索失败：{e}"


def fetch_url(url: str, max_chars: int = 4000):
    """抓一个网页，返回纯文本（粗略去标签）。"""
    try:
        r = requests.get(url, headers=UA, timeout=20)
        html = r.text
        html = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html,
                      flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:int(max_chars)]
    except Exception as e:
        return f"抓取失败：{e}"


# ============================================================ 文件（沙箱内）
def _safe_path(rel: str) -> str:
    """把相对路径解析到沙箱内，越界就报错。"""
    os.makedirs(SANDBOX, exist_ok=True)
    path = os.path.abspath(os.path.join(SANDBOX, rel))
    if not path.startswith(os.path.abspath(SANDBOX) + os.sep):
        raise ValueError("路径越界：只能操作 workspace 目录内的文件")
    return path


def list_files(subdir: str = "."):
    """列出沙箱目录里的文件。"""
    path = _safe_path(subdir)
    if not os.path.isdir(path):
        return f"不是目录：{subdir}"
    return "\n".join(sorted(os.listdir(path))) or "（空目录）"


def read_file(path: str):
    """读取沙箱里的一个文本文件。"""
    with open(_safe_path(path), encoding="utf-8") as f:
        return f.read()[:8000]


def write_file(path: str, content: str):
    """写入（覆盖）沙箱里的一个文本文件。"""
    p = _safe_path(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {path}（{len(content)} 字符）"


# ============================================================ 执行代码（默认关）
def run_python(code: str):
    """在子进程里跑一段 Python，返回输出。默认禁用，除非设了 ENABLE_CODE_EXEC=1。"""
    if os.getenv("ENABLE_CODE_EXEC") != "1":
        return "执行代码已被禁用。若确实要用，请设置环境变量 ENABLE_CODE_EXEC=1。"
    try:
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=15,
                              cwd=SANDBOX)
        out = (proc.stdout or "") + (proc.stderr or "")
        return out.strip()[:4000] or "（无输出）"
    except subprocess.TimeoutExpired:
        return "执行超时（15 秒上限）。"


# ============================================================ 打包
def extra_tools():
    """进阶工具箱。想关掉某个，从这里删掉对应 Tool 即可。"""
    return [
        Tool("web_search", "联网搜索，返回标题和链接。",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "max_results": {"type": "integer", "default": 5}},
              "required": ["query"]}, web_search),
        Tool("fetch_url", "抓取一个网页并返回正文文本。",
             {"type": "object",
              "properties": {"url": {"type": "string"}},
              "required": ["url"]}, fetch_url),
        Tool("list_files", "列出 workspace 目录里的文件。",
             {"type": "object",
              "properties": {"subdir": {"type": "string", "default": "."}}},
             list_files),
        Tool("read_file", "读取 workspace 目录里的文本文件。",
             {"type": "object",
              "properties": {"path": {"type": "string"}},
              "required": ["path"]}, read_file),
        Tool("write_file", "在 workspace 目录里写入一个文本文件。",
             {"type": "object",
              "properties": {"path": {"type": "string"},
                             "content": {"type": "string"}},
              "required": ["path", "content"]}, write_file),
        Tool("run_python", "执行一段 Python 代码并返回输出（默认禁用）。",
             {"type": "object",
              "properties": {"code": {"type": "string"}},
              "required": ["code"]}, run_python),
    ]
