#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智能体核心：LLM + 工具 + 循环 + 记忆。

只依赖 requests 和标准库，不装 openai 也能跑。
「个人助理」和「游戏 NPC」共用这一份引擎，区别只在「人设」和「给的工具有哪些」。
"""

import json
import os
import requests


# ----------------------------------------------------------------- 工具定义
class Tool:
    """一个工具 = 一个普通 Python 函数 + 给模型看的说明书。"""

    def __init__(self, name, description, parameters, func):
        self.name = name
        self.description = description      # 模型靠这个判断「什么时候用」
        self.parameters = parameters        # JSON Schema，描述参数
        self.func = func                    # 真正干活的函数

    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, args: dict) -> str:
        """执行工具。参数以关键字传入，出错也返回字符串（不崩掉整个循环）。"""
        try:
            result = self.func(**args)
            return result if isinstance(result, str) else json.dumps(
                result, ensure_ascii=False)
        except Exception as e:  # 工具炸了也要把错误喂回模型，让它自己决定怎么办
            return f"[工具 {self.name} 出错] {type(e).__name__}: {e}"


# ----------------------------------------------------------------- LLM 客户端
class LLMClient:
    """一个最小的 OpenAI 兼容客户端（Chat Completions）。"""

    def __init__(self, api_key=None, base_url=None, model=None, timeout=60):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = timeout
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0}

    def ready(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages, tools=None, temperature=0.7):
        """发一轮请求，返回 assistant message（dict）。

        顺便把 token 用量记在 self.last_usage 里（OpenAI 兼容接口都会返回 usage）。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        r = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f"接口返回 {r.status_code}: {r.text[:500]}")

        data = r.json()
        u = data.get("usage") or {}
        self.last_usage = {
            "prompt_tokens": int(u.get("prompt_tokens") or 0),
            "completion_tokens": int(u.get("completion_tokens") or 0),
            "total_tokens": int(u.get("total_tokens") or 0),
        }
        return data["choices"][0]["message"]


# ----------------------------------------------------------------- 智能体
class Agent:
    """会自己调用工具、自己循环干活的智能体。

    典型用法::

        agent = Agent(client, system_prompt="你是...", tools=[...])
        print(agent.chat("北京天气怎么样？"))
        agent.reset()          # 清空记忆，重开一段对话
        agent.history          # 当前完整上下文（游戏里可存档）
    """

    def __init__(self, client: LLMClient, system_prompt: str,
                 tools=None, temperature=0.7, max_steps=6,
                 context_provider=None,
                 memory_turns=30, summarize_batch=6):
        self.client = client
        self.system_prompt = system_prompt
        self.tools = {t.name: t for t in (tools or [])}
        self.temperature = temperature
        self.max_steps = max_steps        # 循环上限：防止它烧钱烧到天亮
        self.messages = []                # 完整历史（永不被裁剪，存档存的就是它）
        # 每一轮把「实时状态」拼进 system，让 NPC 知道玩家现在多少钱、多少血
        self.context_provider = context_provider
        # ---- 记忆窗口 ----
        self.memory_turns = memory_turns  # 发给大模型的上下文只带最近这么多轮
        self.summarize_batch = summarize_batch   # 攒够几轮才压一次摘要（省 API 调用）
        self.summary = ""                 # 老对话压成的一句话摘要
        self.summarized_upto = 0          # messages[:这个下标] 已经被压进摘要了
        # ---- 用量统计 ----
        self.turn_usage = self._empty_usage()      # 本轮（一次 chat）的累计
        self.usage_total = self._empty_usage()     # 这个 Agent 一生的累计

    @staticmethod
    def _empty_usage():
        return {"calls": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0}

    def _llm(self, messages, tools=None, temperature=None):
        """包一层 client.chat，顺便累计 token 用量。"""
        msg = self.client.chat(
            messages, tools=tools,
            temperature=self.temperature if temperature is None else temperature,
        )
        u = getattr(self.client, "last_usage", None) or {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = int(u.get(k) or 0)
            self.turn_usage[k] += v
            self.usage_total[k] += v
        self.turn_usage["calls"] += 1
        self.usage_total["calls"] += 1
        return msg

    # -------------------------------------------------- 记忆
    def reset(self):
        """清空对话记忆（换一段对话 / 游戏重开）。人设不变。"""
        self.messages = []
        self.summary = ""
        self.summarized_upto = 0

    @property
    def history(self):
        return self.messages

    def load_history(self, messages, summary="", summarized_upto=None):
        """载入之前存下来的上下文（存档 / 接回进度）。"""
        self.messages = list(messages)
        self.summary = summary or ""
        # 没记录过进度就从 0 开始，让下次对话把超窗口的老内容压成摘要
        self.summarized_upto = 0 if summarized_upto is None \
            else int(summarized_upto)

    # -------------------------------------------------- 记忆窗口
    def _window_start(self):
        """算出「最近 memory_turns 轮」从哪条消息开始。

        一轮 = 一条 user 消息 + 后面的若干条回复/工具调用。
        """
        starts = [i for i, m in enumerate(self.messages)
                  if m.get("role") == "user"]
        if len(starts) <= self.memory_turns:
            return 0
        return starts[len(starts) - self.memory_turns]

    def _maybe_summarize(self, upto):
        """把掉出窗口、且还没压过的老对话，压成一句话摘要。"""
        if upto <= self.summarized_upto:
            return
        chunk = self.messages[self.summarized_upto:upto]
        turns = sum(1 for m in chunk if m.get("role") == "user")
        if turns == 0:
            self.summarized_upto = upto
            return
        if turns < self.summarize_batch:
            return                        # 攒着，下次一起压（省一次 API 调用）
        self.summary = self._make_summary(chunk)
        self.summarized_upto = upto

    def _make_summary(self, chunk):
        """让大模型把一段老对话合并进已有摘要。失败就保持原摘要。"""
        lines = []
        for m in chunk:
            role, content = m.get("role"), m.get("content")
            if not content:
                continue
            if role == "user":
                lines.append(f"玩家：{content}")
            elif role == "assistant":
                lines.append(f"角色：{content}")
        if not lines:
            return self.summary

        prompt = (
            "你在整理一段角色扮演对话的长期记忆。\n"
            f"已有摘要：{self.summary or '（无）'}\n"
            "新增对话：\n" + "\n".join(lines)[-4000:] + "\n\n"
            "请把新增内容合并进摘要，输出更新后的摘要。要求：\n"
            "- 只用一句话，中文，不超过 60 字。\n"
            "- 只保留以后对话还有用的信息：玩家称呼、买过/拥有什么、"
            "欠谁钱、答应过什么、重要事件。\n"
            "- 不要写对话过程，别写「玩家说」「角色说」，不要编造。\n"
            "直接输出那句话，不要任何前缀。"
        )
        try:
            msg = self._llm([{"role": "user", "content": prompt}],
                            temperature=0)
            text = (msg.get("content") or "").strip()
            return text or self.summary
        except Exception as e:
            print(f"[摘要失败] {e}")
            return self.summary

    def compress_now(self, keep_turns=3):
        """手动把当前对话压成长期记忆（不用等窗口溢出）。

        保留最近 keep_turns 轮原文，更早的内容合并进摘要。
        会话很短（轮数 <= keep_turns）时，就把全部内容压进去。
        返回是否真的压了东西。
        """
        starts = [i for i, m in enumerate(self.messages)
                  if m.get("role") == "user"]
        if len(starts) > keep_turns:
            upto = starts[len(starts) - keep_turns]
        else:
            upto = len(self.messages)

        if upto <= self.summarized_upto:
            return False                      # 没有新东西可压
        chunk = self.messages[self.summarized_upto:upto]
        self.summary = self._make_summary(chunk)
        self.summarized_upto = upto
        return True

    def _build_context(self):
        """拼出发给大模型的消息：system + 记忆摘要 + 最近 N 轮。"""
        upto = self._window_start()
        self._maybe_summarize(upto)

        system = self.system_prompt
        if self.context_provider:
            extra = self.context_provider()
            if extra:
                system = f"{system}\n\n【当前实时状态 · 每轮刷新】\n{extra}"

        head = [{"role": "system", "content": system}]
        if self.summary:
            head.append({"role": "system",
                         "content": f"【你对玩家过去的记忆（摘要）】{self.summary}"})
        return head + self.messages[upto:]

    def _base_messages(self):
        return self._build_context()

    def _tool_schemas(self):
        return [t.schema() for t in self.tools.values()] or None

    # -------------------------------------------------- 主循环
    def chat(self, user_text: str) -> str:
        """给一句话，跑完一整个循环，返回最终的文本回复。"""
        self.turn_usage = self._empty_usage()      # 每轮重新计
        self.messages.append({"role": "user", "content": user_text})

        for step in range(self.max_steps):
            msg = self._llm(
                self._base_messages(),
                tools=self._tool_schemas(),
            )
            self.messages.append(msg)

            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                return msg.get("content") or ""

            # 逐个执行工具，把结果喂回上下文，再进入下一轮
            for call in tool_calls:
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool = self.tools.get(fn["name"])
                result = tool.run(args) if tool else f"[没有名为 {fn['name']} 的工具]"
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                })

        # 达到步数上限还没收尾，兜一句
        return "（思考步数超过上限，先停在这里。）"
