#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 NPC 嵌进游戏循环的示例（这里是文字版，逻辑和图形游戏一模一样）。

要点：
1. NPC 是一个 Agent，构造一次就复用，记忆（历史）会保留。
2. NPC 的工具直接读写游戏 state，所以对话能真正改变游戏。
3. 图形游戏里，把这套东西放进「点击 NPC → 弹出对话框」的事件里即可。
"""

from agent_core import Agent, LLMClient
from personas import get_npc
from tools import build_npc_tools, diff_state, format_changes


def main():
    client = LLMClient()

    # —— 游戏的全局状态：血量、金币、背包、任务进度都放这儿 ——
    game_state = {
        "player_name": "阿飞",
        "player_hp": 45,
        "gold": 80,
        "inventory": ["破木剑"],
        "quests": {"找猫": "进行中"},
    }

    # —— 造一个 NPC，并把它绑定到游戏状态上（带上价目表）——
    npc = get_npc("blacksmith")
    blacksmith = Agent(
        client,
        system_prompt=npc["prompt"],
        tools=build_npc_tools(game_state, npc["goods"], npc["services"]),
    )

    # —— 模拟玩家点开铁匠说话 ——
    player_lines = [
        "老板，我这把剑能修吗？顺便看看有什么好货。",
        "太贵了，来把铁剑吧，我买了。",
        "那我全都要，钱不是问题！",
    ]

    for line in player_lines:
        before = {"gold": game_state["gold"], "player_hp": game_state["player_hp"],
                  "inventory": list(game_state["inventory"])}
        print(f"阿飞 > {line}")
        reply = blacksmith.chat(line)
        print(f"{npc['name']} > {reply}")
        ch = diff_state(before, game_state)
        if ch:
            print(f"   [状态变化] {format_changes(ch)}")
        print(f"   [当前] 金币={game_state['gold']} "
              f"背包={game_state['inventory']}\n")

    # —— 存档：把对话历史存下来，下次读回来接着聊 ——
    saved = blacksmith.history
    print(f"[存档] 共记录 {len(saved)} 条上下文消息。")

    # 读档（换个人物、重开时用）：
    # blacksmith.load_history(saved)
    # blacksmith.reset()          # 或者直接清空


if __name__ == "__main__":
    main()
