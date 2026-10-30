#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构体检：找出「放错位置的代码 / 重复定义 / 死代码」。

专门针对一类 bug —— 代码被插到类中间，把类截断了
（表现：所有 POST 请求返回 501 Unsupported method）。

两种用法：
    python healthcheck.py          # 命令行单独跑
    import healthcheck; healthcheck.run_checks()   # 或由 selftest 调用
"""

import ast
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def run_checks():
    """做全套检查。返回 (problems, notes)，不打印也不退出。"""
    problems = []
    notes = []

    py_files = sorted(f for f in os.listdir(HERE)
                      if f.endswith(".py") and not f.startswith("_"))

    corpus = "\n".join(_read(os.path.join(HERE, f)) for f in py_files)
    index = os.path.join(HERE, "web", "index.html")
    if os.path.exists(index):
        corpus += "\n" + _read(index)

    def count_uses(name):
        # 允许前面有 "xxx."（personas.remove_character 也算引用）
        return len(re.findall(r"(?<![\w])" + re.escape(name) + r"(?![\w])",
                              corpus))

    def is_self_method(node):
        a = node.args.args
        return bool(a) and a[0].arg == "self"

    for f in py_files:
        src = _read(os.path.join(HERE, f))
        try:
            tree = ast.parse(src, f)
        except SyntaxError as e:
            problems.append(f"{f}: 语法错误 {e}")
            continue

        # ---- return 之后的死代码（曾经把工具列表写在这后面，工具静默丢失）----
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for st in fn.body[:-1]:
                if isinstance(st, ast.Return):
                    problems.append(
                        f"{f}: 函数「{fn.name}」第 {st.lineno} 行 return "
                        f"之后还有代码，永远不会执行")
                    break

        # ---- 模块级定义 ----
        mod_names = defaultdict(list)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                mod_names[node.name].append(node.lineno)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        mod_names[t.id].append(node.lineno)
            elif isinstance(node, ast.AnnAssign) \
                    and isinstance(node.target, ast.Name):
                mod_names[node.target.id].append(node.lineno)

        for name, lines in sorted(mod_names.items()):
            if len(lines) > 1 and not name.startswith("__"):
                problems.append(f"{f}: 模块级重复定义「{name}」于行 {lines}")

        # ---- 关键：模块级函数第一个参数却是 self（本该是类的方法）----
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and is_self_method(node):
                problems.append(
                    f"{f}: 模块级函数「{node.name}」第一个参数是 self"
                    f"（行 {node.lineno}）—— 它本该属于某个类！")

        # ---- 关键：函数里嵌套定义「像方法」的函数（类被截断的症状）----
        for outer in ast.walk(tree):
            if not isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in outer.body:
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and (is_self_method(inner) or inner.name.startswith("do_")):
                    problems.append(
                        f"{f}: 函数「{outer.name}」里嵌套定义了"
                        f"「{inner.name}」（行 {inner.lineno}）"
                        f"—— 类可能被截断了！")

        # ---- 类：重复方法 / HTTP handler 完整性 ----
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            seen = defaultdict(list)
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    seen[b.name].append(b.lineno)
            for name, lines in sorted(seen.items()):
                if len(lines) > 1:
                    problems.append(
                        f"{f}: 类 {node.name} 里重复定义方法「{name}」于行 {lines}")
            if node.name == "Handler":
                missing = {"do_GET", "do_POST"} - set(seen)
                if missing:
                    problems.append(
                        f"{f}: 类 Handler 缺少 {sorted(missing)} —— 可能被截断了！")
                else:
                    notes.append(f"{f}: 类 Handler 方法齐全 {sorted(seen)}")

        # ---- 死代码：模块级函数/类/变量，全项目无人引用 ----
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if count_uses(node.name) <= 1:
                    problems.append(
                        f"{f}: 函数「{node.name}」没人用（行 {node.lineno}）")
            elif isinstance(node, ast.ClassDef):
                if count_uses(node.name) <= 1:
                    problems.append(
                        f"{f}: 类「{node.name}」没人用（行 {node.lineno}）")
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and not t.id.startswith("__") \
                            and count_uses(t.id) <= 1:
                        problems.append(
                            f"{f}: 变量「{t.id}」没人用（行 {node.lineno}）")

    # ---- 前端检查 ----
    if os.path.exists(index):
        html = _read(index)
        js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))

        fdefs = re.findall(r"^\s*function\s+([A-Za-z_$][\w$]*)\s*\(", js, re.M)
        for name in sorted(set(fdefs)):
            if fdefs.count(name) > 1:
                problems.append(
                    f"web/index.html: JS 函数「{name}」重复定义 "
                    f"{fdefs.count(name)} 次")

        used_ids = set(re.findall(r'getElementById\(["\']([^"\']+)["\']\)', js))
        have_ids = set(re.findall(r'\bid=["\']([^"\']+)["\']', html))
        missing = sorted(used_ids - have_ids)
        if missing:
            problems.append(
                f"web/index.html: JS 引用了不存在的元素 id: {missing}")
        else:
            notes.append(
                f"web/index.html: {len(used_ids)} 个 getElementById 全部有效")

    return problems, notes


def main():
    problems, notes = run_checks()
    print("=" * 62)
    if problems:
        print(f"发现 {len(problems)} 个结构问题：\n")
        for p in problems:
            print("  ⚠️ ", p)
    else:
        print("✅ 没发现结构性问题")
    if notes:
        print("\n参考信息：")
        for n in notes:
            print("  ·", n)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
