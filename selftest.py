#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测：不需要 API key，验证「工具」和「智能体循环」是否正确。

我们用一个假的 LLM 客户端（写死回复），来模拟模型的行为：
第 1 轮假装要调用 calculate，第 2 轮给出最终答案。
如果循环和工具都正常，最后应打印出计算结果。
"""

import json

from agent_core import Agent, Tool
from tools import (assistant_tools, build_npc_tools, diff_state,
                   format_changes)


class FakeClient:
    """假客户端：按预设脚本返回 assistant message。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools=None, temperature=0.7):
        self.calls += 1
        return self.script.pop(0)


def test_tools():
    print("— 测试工具 —")
    tools = {t.name: t for t in assistant_tools()}
    assert tools["calculate"].run({"expression": "12*8+3"}) == "99"
    assert "只支持基础数学运算" in tools["calculate"].run({"expression": "__import__('os')"})
    print("  calculate 正常，且挡住了非法表达式 ✓")
    print(f"  时间: {tools['get_current_time'].run({})}")

    # 安全：工具内部报错不应该抛异常出去
    def boom(**kw):
        raise ValueError("炸了")
    bad = Tool("boom", "x", {"type": "object", "properties": {}}, boom)
    assert "[工具 boom 出错]" in bad.run({})
    print("  工具报错被安全捕获 ✓")


def test_agent_loop():
    print("\n— 测试智能体循环 —")
    script = [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "calculate",
                         "arguments": json.dumps({"expression": "12*8+3"})}}]},
        {"role": "assistant", "content": "结果是 99。"},
    ]
    agent = Agent(FakeClient(script), "你是助理", tools=assistant_tools())
    out = agent.chat("12*8+3 等于多少？")
    assert out == "结果是 99。", out

    # 上下文里应包含：user、assistant(工具调用)、tool(结果)、assistant(终答)
    roles = [m["role"] for m in agent.history]
    assert roles == ["user", "assistant", "tool", "assistant"], roles
    print(f"  循环走通，上下文顺序正确: {roles} ✓")

    agent.reset()
    assert agent.history == []
    print("  reset 清空记忆 ✓")


def test_npc_state():
    print("\n— 测试 NPC 改游戏状态 —")
    state = {"player_hp": 50, "gold": 100, "inventory": []}
    goods = {"铁剑": 50, "钢盾": 200}
    services = {"住店一晚": (20, "heal:40")}
    tools = {t.name: t for t in build_npc_tools(state, goods, services)}

    tools["change_gold"].run({"delta": -30})
    tools["give_item"].run({"item": "铁剑"})
    tools["heal_player"].run({"amount": 20})
    assert state["gold"] == 70, state
    assert state["inventory"] == ["铁剑"], state
    assert state["player_hp"] == 70, state
    print(f"  基础工具正常: {state} ✓")

    # 交易：价格由服务端说了算
    state2 = {"player_hp": 40, "gold": 100, "inventory": []}
    t2 = {t.name: t for t in build_npc_tools(state2, goods, services)}
    before = {"gold": state2["gold"], "player_hp": state2["player_hp"],
              "inventory": list(state2["inventory"])}

    r = t2["buy_item"].run({"item": "铁剑"})
    assert "成交" in r and state2["gold"] == 50, (r, state2)
    print(f"  买铁剑：扣 50 金，余额 {state2['gold']} ✓")

    # 钱不够必须被拒绝
    r = t2["buy_item"].run({"item": "钢盾"})   # 要 200，只有 50
    assert "金币不足" in r and state2["gold"] == 50, (r, state2)
    print("  钱不够被拒绝，余额未变 ✓")

    # 服务：住店回血
    r = t2["buy_service"].run({"service": "住店一晚"})
    assert "生命值恢复到 80" in r, (r, state2)
    print("  住店回血 40 ✓")

    ch = diff_state(before, state2)
    assert {"kind": "gold", "delta": -70} in ch, ch
    assert {"kind": "player_hp", "delta": 40} in ch, ch
    assert {"kind": "item_add", "item": "铁剑"} in ch, ch
    print(f"  变化量计算正确: {format_changes(ch)} ✓")


def test_memory_window():
    print("\n— 测试记忆窗口与摘要 —")

    class FakeClient:
        """假客户端：不带 tools 的调用就是「压缩摘要」。"""
        def __init__(self):
            self.summary_calls = 0

        def chat(self, messages, tools=None, temperature=0.7):
            if tools is None:
                self.summary_calls += 1
                return {"role": "assistant",
                        "content": "玩家叫阿飞，买过铁剑，欠老板娘20金。"}
            return {"role": "assistant", "content": "好的"}

    client = FakeClient()
    agent = Agent(client, "你是NPC", memory_turns=3, summarize_batch=2)
    for i in range(6):                     # 造 6 轮对话
        agent.messages.append({"role": "user", "content": f"第{i}句"})
        agent.messages.append({"role": "assistant", "content": f"回{i}"})

    ctx = agent._build_context()           # 触发窗口计算 + 摘要

    users = [m["content"] for m in ctx if m["role"] == "user"]
    assert users == ["第3句", "第4句", "第5句"], users
    print(f"  只发最近 3 轮：{users} ✓")

    assert agent.summary, "摘要没生成"
    assert any("摘要" in m["content"] for m in ctx if m["role"] == "system")
    print(f"  摘要已生成并塞进上下文：{agent.summary} ✓")

    assert len(agent.messages) == 12, len(agent.messages)
    print(f"  完整历史仍保留 {len(agent.messages)} 条（没被裁掉）✓")

    # 存档 → 读档，摘要要跟着回来
    saved_msgs, saved_sum = list(agent.messages), agent.summary
    saved_upto = agent.summarized_upto
    agent2 = Agent(client, "你是NPC", memory_turns=3, summarize_batch=2)
    agent2.load_history(saved_msgs, saved_sum, saved_upto)
    assert agent2.summary == saved_sum and len(agent2.messages) == 12
    print("  存档/读档后摘要与历史都在 ✓")

    # 再聊一轮，不该重复压缩（省一次 API 调用）
    before = client.summary_calls
    agent2.messages.append({"role": "user", "content": "新的一句"})
    agent2.messages.append({"role": "assistant", "content": "新回复"})
    agent2._build_context()
    assert client.summary_calls == before, "重复压缩了，浪费钱"
    print("  窗口滑动时不会重复压缩（省钱）✓")

    agent.reset()
    assert agent.messages == [] and agent.summary == ""
    print("  reset 连摘要一起清空 ✓")

    # 手动「立即整理记忆」：窗口再大也能随时压
    c2 = FakeClient()
    a3 = Agent(c2, "你是NPC", memory_turns=999, summarize_batch=999)
    for i in range(6):
        a3.messages.append({"role": "user", "content": f"第{i}句"})
        a3.messages.append({"role": "assistant", "content": f"回{i}"})
    assert a3.summary == "", "窗口没溢出，不该有摘要"
    assert a3.compress_now(keep_turns=2) is True
    assert a3.summary, "手动整理没生成摘要"
    assert a3.summarized_upto == 8, a3.summarized_upto   # 压了前 4 轮，留最近 2 轮
    assert a3.compress_now(keep_turns=2) is False        # 没新东西，别浪费 API
    print("  手动「立即整理记忆」正常 ✓")


def test_structure():
    print("\n— 结构体检（放错位置 / 重复定义 / 死代码）—")
    import healthcheck
    problems, notes = healthcheck.run_checks()
    for n in notes:
        print(f"    · {n}")
    if problems:
        for p in problems:
            print(f"    ⚠️  {p}")
        raise AssertionError(f"发现 {len(problems)} 个结构问题")
    print("  没有放错位置 / 重复定义 / 死代码 ✓")


if __name__ == "__main__":
    test_tools()
    test_agent_loop()
    test_npc_state()
    test_memory_window()
    test_structure()
    print("\n全部通过 ✅")
