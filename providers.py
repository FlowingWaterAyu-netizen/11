#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国内模型预设：换底座不用改代码，选一个预设即可。

用法（命令行）：
    python providers.py                # 列出所有预设
    python providers.py deepseek       # 把配置写进 .env
    python providers.py --show         # 显示当前 .env
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, ".env")

# 注：模型名会随厂商更新，若调用报「模型不存在」，按报错信息换成官网当前的名字即可。
PROVIDERS = {
    "deepseek": {
        "OPENAI_BASE_URL": "https://api.deepseek.com/v1",
        "OPENAI_MODEL": "deepseek-chat",
        "note": "DeepSeek 官方。也可试 deepseek-reasoner（推理）/ 更新的 v4 系列。",
        "key_page": "https://platform.deepseek.com/",
    },
    "kimi": {
        "OPENAI_BASE_URL": "https://api.moonshot.cn/v1",
        "OPENAI_MODEL": "moonshot-v1-8k",
        "note": "月之暗面 Kimi。新版可用 kimi-k2 等，长上下文和 Agent 任务强。",
        "key_page": "https://platform.moonshot.cn/",
    },
    "qwen": {
        "OPENAI_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "OPENAI_MODEL": "qwen-plus",
        "note": "阿里云百炼 通义千问。也可试 qwen-max / qwen-turbo。",
        "key_page": "https://bailian.console.aliyun.com/",
    },
    "glm": {
        "OPENAI_BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
        "OPENAI_MODEL": "glm-4-flash",
        "note": "智谱 GLM。glm-4-flash 便宜，glm-4-plus 更强。",
        "key_page": "https://bigmodel.cn/",
    },
    "openai": {
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_MODEL": "gpt-4o-mini",
        "note": "OpenAI 官方（国内网络可能不通）。",
        "key_page": "https://platform.openai.com/",
    },
}


def read_env():
    data = {}
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    return data


def write_env(updates: dict):
    data = read_env()
    data.update(updates)
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("# 由 providers.py 生成\n")
        for k, v in data.items():
            f.write(f"{k}={v}\n")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("可用预设：")
        for k, v in PROVIDERS.items():
            print(f"  {k:10s} {v['OPENAI_MODEL']:16s} {v['note']}")
        print("\n用法: python providers.py <预设名>   # 写入 .env")
        return

    if args[0] == "--show":
        for k, v in read_env().items():
            if "KEY" in k and v:
                v = v[:6] + "****"
            print(f"{k}={v}")
        return

    name = args[0]
    if name not in PROVIDERS:
        print(f"没有预设「{name}」，可用：{', '.join(PROVIDERS)}")
        return
    preset = PROVIDERS[name]
    write_env({"OPENAI_BASE_URL": preset["OPENAI_BASE_URL"],
               "OPENAI_MODEL": preset["OPENAI_MODEL"]})
    print(f"已切换到 {name}：")
    print(f"  OPENAI_BASE_URL = {preset['OPENAI_BASE_URL']}")
    print(f"  OPENAI_MODEL    = {preset['OPENAI_MODEL']}")
    print(f"  {preset['note']}")
    print(f"  去这里拿 key：{preset['key_page']}")
    print("\n记得把 OPENAI_API_KEY 填成对应厂商的 key（.env 里改）。")


if __name__ == "__main__":
    main()
