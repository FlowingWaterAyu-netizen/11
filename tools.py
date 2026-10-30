#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具集：助理用的 + NPC 用的。

工具就是普通函数，包一层 Tool 就能给模型调用。
想加新能力？照着下面写一个函数 + 一个 Tool 就行。
"""

import ast
import json
import operator
import os
from datetime import datetime

import requests

from agent_core import Tool

HERE = os.path.dirname(os.path.abspath(__file__))
NOTES_FILE = os.path.join(HERE, "notes.json")


# ============================================================ 助理工具
def get_current_time():
    """返回当前日期和时间。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S (%A)")


# --- 安全计算器：只允许数学运算，不用 eval ---
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        return _OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        return _OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("只支持基础数学运算")


def calculate(expression: str):
    """算数学表达式，比如 '12*8+3' 。"""
    return _eval_node(ast.parse(expression, mode="eval"))


_local_weather = None          # 由 server 注入：() -> 本机所在地天气描述


def set_local_weather(fn):
    """让 get_weather 不传城市时能查到「你所在的地方」。"""
    global _local_weather
    _local_weather = fn


def get_weather(city: str = ""):
    """查城市天气（走 wttr.in，需要联网）。不传城市就查本机所在地。"""
    city = (city or "").strip()
    if not city and _local_weather:
        return _local_weather()
    try:
        r = requests.get(f"https://wttr.in/{city or 'beijing'}",
                         params={"format": "3"},
                         timeout=10, headers={"User-Agent": "curl"})
        return r.text.strip() or f"没查到 {city or '当地'} 的天气"
    except Exception as e:
        return f"查天气失败（{e}），可以稍后再试"


# --- 便签：给助理一个最朴素的「记忆」 ---
def _load_notes():
    if os.path.exists(NOTES_FILE):
        try:
            with open(NOTES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_note(text: str):
    """记一条便签。"""
    notes = _load_notes()
    item = {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": text}
    notes.append(item)
    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)
    return f"已记录：{text}"


def list_notes():
    """列出所有便签。"""
    notes = _load_notes()
    if not notes:
        return "还没有任何便签。"
    return "\n".join(f"[{n['time']}] {n['text']}" for n in notes)


def assistant_tools():
    """助理模式的工具箱。"""
    return [
        Tool("get_current_time", "获取当前日期和时间。",
             {"type": "object", "properties": {}}, get_current_time),
        Tool("calculate", "做数学计算，输入一个算式字符串。",
             {"type": "object",
              "properties": {"expression": {"type": "string",
                                            "description": "如 12*8+3"}},
              "required": ["expression"]}, calculate),
        Tool("get_weather",
             "查当前天气。用户没说城市时不要传 city，直接查用户所在地。",
             {"type": "object",
              "properties": {"city": {"type": "string",
                                      "description": "城市名；省略则查本机所在地"}},
              "required": []}, get_weather),
        Tool("save_note", "把一件事记成便签，方便以后查看。",
             {"type": "object",
              "properties": {"text": {"type": "string"}},
              "required": ["text"]}, save_note),
        Tool("list_notes", "列出所有已记录的便签。",
             {"type": "object", "properties": {}}, list_notes),
    ]


def diff_state(before, after):
    """对比前后状态，算出给玩家带来的变化（前端做动画、命令行打印都用它）。"""
    changes = []
    for field in ("gold", "player_hp"):
        d = after.get(field, 0) - before.get(field, 0)
        if d:
            changes.append({"kind": field, "delta": d})
    b, a = list(before.get("inventory", [])), list(after.get("inventory", []))
    for item in a:
        if item in b:
            b.remove(item)
        else:
            changes.append({"kind": "item_add", "item": item})
    for item in b:
        changes.append({"kind": "item_remove", "item": item})
    return changes


def format_changes(changes):
    """把变化列表转成一行可读文字，例如：💰-50  ❤️+20  🎒+铁剑"""
    icons = {"gold": "💰", "player_hp": "❤️"}
    out = []
    for c in changes:
        if c["kind"] in icons:
            out.append(f"{icons[c['kind']]}{c['delta']:+d}")
        elif c["kind"] == "item_add":
            out.append(f"🎒+{c['item']}")
        elif c["kind"] == "item_remove":
            out.append(f"🎒-{c['item']}")
    return "  ".join(out)


# ============================================================ NPC 工具
# NPC 的工具直接读写游戏状态，这样游戏和对话就能联动。
# 价格表由服务端（personas.py）提供，模型无权改价，也不能白送。
def build_npc_tools(state: dict, goods=None, services=None, mood_holder=None,
                    spells=None, cast_fn=None):
    """给 NPC 造一套工具。

    state       : 游戏共享状态字典，如
                  {"player_name":"阿飞","player_hp":80,"gold":30,"inventory":[]}
    goods       : 商品价目表 {名称: 价格}
    services    : 服务价目表 {名称: (价格, 效果)}，效果如 "heal:40" / "repair" / "info"
    mood_holder : 传一个 dict 进去，NPC 调 show_expression 时会写进去 mood
    spells      : 这个角色会的法术 [{key,name,cost,effect,desc}]
    cast_fn     : 真正执行施法的函数（由 server 注入，负责扣位和算伤害）
    """
    goods = goods or {}
    services = services or {}

    def _fmt(table):
        return "、".join(f"{k}({v if not isinstance(v, tuple) else v[0]}金)"
                         for k, v in table.items()) or "（无）"

    def _match(name, table):
        """先精确匹配，再模糊匹配，返回 (标准名, 值) 或 (None, None)。"""
        if name in table:
            return name, table[name]
        for k, v in table.items():
            if name and (name in k or k in name):
                return k, v
        return None, None

    # -------------------------------------------------- 查询
    def get_player_info():
        """查玩家当前状态。"""
        return json.dumps(state, ensure_ascii=False)

    def list_prices():
        """查本店的商品和服务价格。"""
        return (f"商品：{_fmt(goods)}\n服务：{_fmt(services)}")

    # -------------------------------------------------- 交易（价格服务端说了算）
    def buy_item(item: str):
        """卖给玩家一件商品。价格从价目表取，钱不够会被拒绝。"""
        name, price = _match(item, goods)
        if name is None:
            return f"我这没有「{item}」。在售：{_fmt(goods)}"
        if state.get("gold", 0) < price:
            return (f"【金币不足】{name}要 {price} 金币，"
                    f"玩家只有 {state.get('gold', 0)}。交易失败。")
        state["gold"] = state.get("gold", 0) - price
        state.setdefault("inventory", []).append(name)
        return (f"成交！玩家买下「{name}」，花费 {price} 金币，"
                f"余额 {state['gold']}。背包：{state['inventory']}")

    def buy_service(service: str):
        """卖一项服务给玩家（住店/修理/指点等）。同样会校验金币。"""
        name, entry = _match(service, services)
        if name is None:
            return f"我这不提供「{service}」。可提供：{_fmt(services)}"
        price, effect = entry
        if state.get("gold", 0) < price:
            return (f"【金币不足】{name}要 {price} 金币，"
                    f"玩家只有 {state.get('gold', 0)}。交易失败。")
        state["gold"] = state.get("gold", 0) - price

        if effect.startswith("heal:"):
            amount = int(effect.split(":")[1])
            state["player_hp"] = min(100, state.get("player_hp", 100) + amount)
            return (f"{name}完成，花费 {price} 金币，"
                    f"玩家生命值恢复到 {state['player_hp']}。")
        if effect == "repair":
            return f"{name}完成，花费 {price} 金币，装备焕然一新。"
        return f"{name}完成，花费 {price} 金币，余额 {state['gold']}。"

    # -------------------------------------------------- 通用（给剧情/任务用）
    def give_item(item: str, count: int = 1):
        """直接给玩家物品（任务奖励等，不走金币）。"""
        state.setdefault("inventory", [])
        state["inventory"].extend([item] * int(count))
        return f"已给玩家 {count} 个「{item}」。背包：{state['inventory']}"

    def change_gold(delta: int):
        """增减玩家金币（正数增加、负数扣除），用于悬赏、罚金等。"""
        state["gold"] = state.get("gold", 0) + int(delta)
        return f"玩家金币变为 {state['gold']}。"

    def heal_player(amount: int):
        """给玩家回血（不涉及金币）。"""
        state["player_hp"] = min(100, state.get("player_hp", 100) + int(amount))
        return f"玩家血量恢复，现在是 {state['player_hp']}。"

    # -------------------------------------------------- 表情
    def show_expression(emotion: str):
        """切换你的表情。可选：neutral（平静）/ happy（开心）/ angry（生气）。"""
        emo = str(emotion).strip().lower()
        if emo not in ("neutral", "happy", "angry"):
            emo = "neutral"
        if mood_holder is not None:
            mood_holder["mood"] = emo
        return f"（你的表情变成了 {emo}）"

    tools = [
        Tool("get_player_info", "查看玩家当前的状态（血量、金币、背包等）。",
             {"type": "object", "properties": {}}, get_player_info),
        Tool("list_prices", "查看本店所有商品和服务的价格。",
             {"type": "object", "properties": {}}, list_prices),
        Tool("buy_item", "卖一件商品给玩家。价格自动从价目表取，钱不够会失败。",
             {"type": "object",
              "properties": {"item": {"type": "string",
                                      "description": "商品名称"}},
              "required": ["item"]}, buy_item),
        Tool("buy_service", "卖一项服务给玩家（住店、修理、指点等）。",
             {"type": "object",
              "properties": {"service": {"type": "string",
                                         "description": "服务名称"}},
              "required": ["service"]}, buy_service),
        Tool("give_item", "直接给玩家物品（任务奖励，不走金币）。",
             {"type": "object",
              "properties": {"item": {"type": "string"},
                             "count": {"type": "integer", "default": 1}},
              "required": ["item"]}, give_item),
        Tool("change_gold", "增减玩家金币，用于悬赏、罚金等。",
             {"type": "object",
              "properties": {"delta": {"type": "integer"}},
              "required": ["delta"]}, change_gold),
        Tool("heal_player", "给玩家恢复生命值（不涉及金币）。",
             {"type": "object",
              "properties": {"amount": {"type": "integer"}},
              "required": ["amount"]}, heal_player),
        Tool("show_expression", "切换你的表情，表达当前情绪。"
             "交易成功/高兴时用 happy，被砍价/生气时用 angry，平时 neutral。",
             {"type": "object",
              "properties": {"emotion": {
                  "type": "string", "enum": ["neutral", "happy", "angry"],
                  "description": "neutral 平静 / happy 开心 / angry 生气"}},
              "required": ["emotion"]}, show_expression),
    ]

    if spells and cast_fn:
        effect_zh = {"damage": "伤害", "heal": "治疗", "utility": "场面"}
        desc = "、".join(
            f"{s['key']}={s['name']}({s.get('cost', 1)}位·"
            f"{effect_zh.get(s.get('effect'), '场面')})"
            for s in spells)
        tools.append(Tool(
            "cast_spell",
            f"施放你会的一个法术。你会：{desc}。"
            "玩家明确要求施法时**必须真的调用这个工具**，不能只在台词里说「我放了」；"
            "没被要求时不要自己放；也不要编造列表里没有的法术。",
            {"type": "object",
             "properties": {
                 "spell": {"type": "string",
                           "enum": [s["key"] for s in spells],
                           "description": "法术 key（见工具说明里的 key=名字）"},
                 "target": {"type": "string",
                            "description": "player（玩家）或角色 key；纯场面法术可省略"}},
             "required": ["spell"]},
            cast_fn))

    return tools
