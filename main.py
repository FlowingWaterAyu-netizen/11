#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口：同一个引擎，两种玩法。

用法：
    python main.py                       # 助理模式（默认）
    python main.py --npc blacksmith      # 扮演铁匠 NPC
    python main.py --npc innkeeper -v    # 让电脑念出来（macOS 语音）
    python main.py --list                # 看看有哪些 NPC

配置（环境变量，或直接写进 .env）：
    OPENAI_API_KEY   你的 key（必填）
    OPENAI_BASE_URL  接口地址，默认 https://api.openai.com/v1
    OPENAI_MODEL     模型名，默认 gpt-4o-mini
"""

import argparse
import os
import subprocess
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from agent_core import Agent, LLMClient
from personas import ASSISTANT_SYSTEM, NPC_PERSONAS, get_npc
from tools import assistant_tools, build_npc_tools, diff_state, format_changes
from tools_extra import extra_tools


def speak(text, voice="Tingting"):
    """用 macOS 自带语音念出来（可选功能）。"""
    try:
        subprocess.run(["say", "-v", voice, text], check=False)
    except FileNotFoundError:
        print("[提示] 这台机器没有 say 命令，跳过语音。")


def run_assistant(client, voice):
    agent = Agent(client, ASSISTANT_SYSTEM,
                  tools=assistant_tools() + extra_tools())
    print("=" * 46)
    print("  小助 · 个人助理模式   （输入 /quit 退出，/reset 清空记忆）")
    print("=" * 46)
    while True:
        try:
            user = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user in ("/quit", "/exit", "退出"):
            break
        if user == "/reset":
            agent.reset()
            print("（记忆已清空）")
            continue
        reply = agent.chat(user)
        print(f"小助 > {reply}")
        if voice:
            speak(reply)


def run_npc(client, key, voice):
    npc = get_npc(key)
    if not npc:
        print(f"没有叫「{key}」的 NPC。可用：{', '.join(NPC_PERSONAS)}")
        return

    # 游戏状态：NPC 的工具会直接改它，游戏循环读它就知道发生了什么
    state = {"player_name": "旅人", "player_hp": 60, "gold": 100, "inventory": []}

    def context():
        goods = "、".join(f"{k}({v}金)" for k, v in npc.get("goods", {}).items()) or "无"
        svcs = "、".join(f"{k}({v[0]}金)" for k, v in npc.get("services", {}).items()) or "无"
        return (f"玩家：{state['player_name']}，生命值 {state['player_hp']}/100，"
                f"金币 {state['gold']}，背包 {state['inventory'] or '空'}\n"
                f"本店商品：{goods}\n本店服务：{svcs}")

    agent = Agent(client, npc["prompt"],
                  tools=build_npc_tools(state, npc.get("goods"),
                                        npc.get("services")),
                  context_provider=context)

    print("=" * 46)
    print(f"  {npc['name']}（{npc['title']}）  （/quit 退出，/state 看游戏状态）")
    print("=" * 46)
    while True:
        try:
            user = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user in ("/quit", "/exit", "退出"):
            break
        if user == "/state":
            print("当前状态:", state)
            continue
        before = {"gold": state.get("gold", 0),
                  "player_hp": state.get("player_hp", 0),
                  "inventory": list(state.get("inventory", []))}
        reply = agent.chat(user)
        print(f"{npc['name']} > {reply}")
        ch = diff_state(before, state)
        if ch:
            print(f"   [{format_changes(ch)}]  "
                  f"❤️ {state['player_hp']}  💰 {state['gold']}  "
                  f"🎒 {'、'.join(state['inventory']) or '空'}")
        if voice:
            speak(reply)


def main():
    ap = argparse.ArgumentParser(description="个人助理 / 游戏 NPC（共用一套引擎）")
    ap.add_argument("--npc", metavar="KEY", help="以 NPC 模式启动，填入角色 key")
    ap.add_argument("--list", action="store_true", help="列出所有 NPC 并退出")
    ap.add_argument("-v", "--voice", action="store_true", help="用系统语音念出回复")
    ap.add_argument("--model", help="覆盖模型名")
    args = ap.parse_args()

    if args.list:
        for k, v in NPC_PERSONAS.items():
            print(f"  {k:12s} {v['name']}（{v['title']}）")
        return

    client = LLMClient(model=args.model)
    if not client.ready():
        print("还没有配置 API key。\n"
              "  1) 在 assistant_agent/ 下建一个 .env，写入 OPENAI_API_KEY=你的key\n"
              "  2) 或者直接 export OPENAI_API_KEY=你的key\n"
              "也可以顺手设置 OPENAI_BASE_URL / OPENAI_MODEL 换成别的模型。")
        sys.exit(1)

    if args.npc:
        run_npc(client, args.npc, args.voice)
    else:
        run_assistant(client, args.voice)


if __name__ == "__main__":
    main()
