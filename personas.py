#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色数据层。

设计原则：**角色是数据，不是代码。**

    characters.json   ← 【唯一真源】角色的全部数据都在这
    personas.py       ← 只负责：读写 JSON、拼 prompt 模板、增删角色的工具函数

想加/删/改角色？改 `characters.json`（或者用网页上的角色管理）。
这个文件里没有任何角色数据了 —— 只有模板和读写逻辑。
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CHARACTERS_FILE = os.path.join(HERE, "characters.json")


# ================================================================ 助理人设
ASSISTANT_NAME = "小助"
ASSISTANT_AVATAR = "avatars/assistant.svg"

ASSISTANT_SYSTEM = """你是一个中文个人助理，名叫「小助」。
风格：干练、直接、不说客套废话。

原则：
- 需要查时间 / 算数 / 查天气 / 记便签时，主动调用工具，不要凭空编。
- 不确定的事就说不确定，绝不编造。
- 回答简洁；用户要细节时再展开。
- 记东西时用 save_note；用户问「我之前记了什么」时用 list_notes。
"""


# ================================================================ 说话长度档
# 每个角色可以选一档，决定 prompt 里那句「回复多长」怎么写
VERBOSITY_RULES = {
    "terse":  "回复要极短，一句话，别超过 20 个字。",
    "normal": "回复控制在 1~3 句，像真人对话，别写小作文。",
    "chatty": "你可以放开聊，3~6 句都没问题，可以多讲点见闻、细节和废话；但别写成大段独白。",
}
DEFAULT_VERBOSITY = "normal"
DEFAULT_MEMORY_TURNS = 20          # 发给大模型的上下文，只带最近这么多轮
DEFAULT_SUMMARIZE_BATCH = 6        # 攒够几轮「掉出窗口」的对话，才压一次摘要


def _fmt_samples(samples):
    """把示例台词排成 few-shot。"""
    if not samples:
        return ""
    out = []
    for s in samples:
        if isinstance(s, (list, tuple)) and len(s) >= 2:
            out.append(f"玩家：{s[0]}\n你：{s[1]}")
        elif isinstance(s, str) and s.strip():
            out.append(s.strip())
    return "\n\n".join(out)


def _npc_prompt(name, role, personality, speech, goal="", rules="",
                verbosity=DEFAULT_VERBOSITY, title="", history="",
                catchphrase="", quirk="", loves="", hates="", secret="",
                never="", samples=None):
    """把结构化字段拼成给大模型看的人设。

    原则：用**具体的过去、口头禅、怪癖、示例台词**来建立特色，
    而不是堆形容词。形容词模型会忽略，具体细节它会照做。
    """
    length_rule = VERBOSITY_RULES.get(verbosity, VERBOSITY_RULES[DEFAULT_VERBOSITY])
    samples_txt = _fmt_samples(samples)

    parts = [f"你正在扮演一个游戏角色，玩家就站在你面前。\n"]
    parts.append(f"【你是谁】{name}{'（' + title + '）' if title else ''}。{role}")
    if history:
        parts.append(f"【你的过去】{history}")
    parts.append(f"【性格】{personality}")
    parts.append(f"【你怎么说话】{speech}")
    if catchphrase:
        parts.append(f"【口头禅】{catchphrase}")
    if quirk:
        parts.append(f"【小动作】{quirk}")
    if loves:
        parts.append(f"【你喜欢的】{loves}")
    if hates:
        parts.append(f"【你讨厌的】{hates}")
    if secret:
        parts.append(f"【你不愿提的】{secret}")
    if never:
        parts.append(f"【你绝不会】{never}")
    if goal:
        parts.append(f"【你的目标】{goal}")

    if samples_txt:
        parts.append("\n【你说话的样子】（照着这个感觉说，别照抄字句）\n" + samples_txt)

    parts.append(f"""
规则：
- 你就是这个人，不是 AI、不是助手。绝不承认自己是程序，也不要解释设定。
- {length_rule}
- 用具体的动作、口气、细节体现性格，别直接说「我很豪爽」这种话。
- 玩家问到和你的职业、这个世界有关的事，就按你的身份回答。
- 【重要】玩家要买东西时，必须调用 buy_item；要买服务（住店、修理、指点）时调用 buy_service。
- 【重要】价格由工具决定，你绝不能自己编价格，也不能免费送东西。
- 【重要】如果工具返回「金币不足」，你要如实告诉玩家买不起，不能假装成交。
- 玩家问你他有多少钱/多少血时，调用 get_player_info 查，别瞎猜。
- 【重要】玩家明确要你施法时，必须调用 cast_spell 真的放，不能只用嘴说、也不能拿性格或「不直接给答案」推脱；伤害/治疗数字以工具返回为准，不要自己编。
- 每轮回复前，先调用 show_expression 表明你此刻的情绪：做成买卖/高兴时 happy，
  被砍价到亏本、被耍、被挑衅时 angry，平常 neutral。
{rules}
- 需要查看玩家状态或给东西时，调用工具；不要凭空说「我给了你一把剑」却什么都没发生。""")

    return "\n".join(parts)




# ================================================================ 读写
CHARACTERS = []          # 原始列表（有序，保持 JSON 里的顺序）
NPC_PERSONAS = {}        # key -> 规范化后的条目（多了一个拼好的 prompt）


def _normalize(entry, index=0):
    """补齐默认字段，把 services 的 list 还原成 tuple，并拼好 prompt。"""
    e = dict(entry)
    e.setdefault("key", f"npc{index}")
    e.setdefault("name", e["key"])
    e.setdefault("title", "")
    e.setdefault("avatar", "")
    e.setdefault("verbosity", DEFAULT_VERBOSITY)
    e.setdefault("memory_turns", DEFAULT_MEMORY_TURNS)
    e.setdefault("summarize_batch", DEFAULT_SUMMARIZE_BATCH)
    e.setdefault("goods", {})
    svc = {}
    for k, v in (e.get("services") or {}).items():
        svc[k] = tuple(v) if isinstance(v, (list, tuple)) else v
    e["services"] = svc
    e["prompt"] = _npc_prompt(
        name=e.get("name", ""), role=e.get("role", ""),
        personality=e.get("personality", ""), speech=e.get("speech", ""),
        goal=e.get("goal", ""), rules=e.get("rules", ""),
        verbosity=e.get("verbosity", DEFAULT_VERBOSITY),
        title=e.get("title", ""), history=e.get("history", ""),
        catchphrase=e.get("catchphrase", ""), quirk=e.get("quirk", ""),
        loves=e.get("loves", ""), hates=e.get("hates", ""),
        secret=e.get("secret", ""), never=e.get("never", ""),
        samples=e.get("samples"))
    return e


def write_raw(characters):
    """把角色列表写回 characters.json。"""
    with open(CHARACTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(characters, f, ensure_ascii=False, indent=2)


def load_characters():
    """读取角色表。

    characters.json 是唯一真源 —— 这里不做任何「播种」。
    文件不存在就当作「没有任何 NPC」，并建一个空表方便你直接编辑。
    """
    global CHARACTERS, NPC_PERSONAS

    if not os.path.exists(CHARACTERS_FILE):
        print(f"[角色表] ⚠️  找不到 {os.path.basename(CHARACTERS_FILE)}，"
              f"当前没有任何 NPC。")
        print("         · 想恢复：网页「存档 → 导入备份」导入整包备份")
        print(f"         · 想新建：直接编辑 {os.path.basename(CHARACTERS_FILE)}"
              f"（格式是数组，例如 []）")
        try:
            write_raw([])
        except OSError as e:
            print(f"[角色表] 建空表失败：{e}")
        CHARACTERS, NPC_PERSONAS = [], {}
        return NPC_PERSONAS

    try:
        with open(CHARACTERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):                 # 兼容 {"characters":[...]}
            data = data.get("characters", [])
        if not isinstance(data, list):
            raise ValueError("格式不对，应该是数组")
    except Exception as e:
        print(f"[角色表] ⚠️  读取失败（{e}），当作没有 NPC 处理")
        data = []

    CHARACTERS = data
    NPC_PERSONAS = {n["key"]: n
                    for n in (_normalize(e, i) for i, e in enumerate(data))}
    return NPC_PERSONAS


def get_npc(key):
    """按 key 取一个角色；取不到返回 None。"""
    return NPC_PERSONAS.get(key)


def remove_character(key):
    """从角色表里删掉一条，返回 (原始条目, 原位置)；没有则 (None, -1)。"""
    global CHARACTERS
    for i, e in enumerate(CHARACTERS):
        if e.get("key") == key:
            entry = CHARACTERS.pop(i)
            NPC_PERSONAS.pop(key, None)
            write_raw(CHARACTERS)
            return entry, i
    return None, -1


def insert_character(entry, index=None):
    """把一条角色插回角色表（撤销抹除用）。"""
    global CHARACTERS
    if index is None or index < 0 or index > len(CHARACTERS):
        index = len(CHARACTERS)
    CHARACTERS.insert(index, entry)
    write_raw(CHARACTERS)
    NPC_PERSONAS[entry.get("key")] = _normalize(entry, index)
    return NPC_PERSONAS[entry.get("key")]

# 模块导入时就加载好
load_characters()
