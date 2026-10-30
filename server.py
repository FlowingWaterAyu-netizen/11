#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 HTTP 服务：既是网页聊天后端，也是游戏接入的接口。

启动：
    python server.py            # 默认 http://127.0.0.1:8200
    python server.py --port 9000

接口（游戏里用）：
    GET  /health                        健康检查
    GET  /npcs                          列出所有 NPC
    POST /chat    {session,mode,npc,message} -> {reply,state}
    POST /reset   {session}
    GET  /state?session=xxx             NPC 模式的游戏状态

浏览器打开 http://127.0.0.1:8200 就是聊天界面。
"""

import argparse
import base64
import copy
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime

import requests

import personas as personas_mod
from backup import (build_character_zip, build_zip, import_character,
                    inspect_pack, restore_backup)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from agent_core import Agent, LLMClient
from personas import (ASSISTANT_AVATAR, ASSISTANT_NAME, ASSISTANT_SYSTEM,
                      NPC_PERSONAS, get_npc)
from tools import (assistant_tools, build_npc_tools, diff_state,
                   set_local_weather)
from tools_extra import extra_tools

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
SAVE_DIR = os.path.join(HERE, "saves")      # 每个角色一个 json 存档
os.makedirs(SAVE_DIR, exist_ok=True)
AVATAR_DIR = os.path.join(WEB_DIR, "avatars")
BG_DIR = os.path.join(WEB_DIR, "bg")            # 自定义页面背景
APPEARANCE_THEMES = ("night", "midnight", "ink", "forest", "cyber")
SETTINGS_FILE = os.path.join(HERE, "settings.json")   # 角色启用/停用等设置
MODEL_KEYS_FILE = os.path.join(HERE, "model_keys.json")  # 角色专属 API key（不进备份）
USAGE_FILE = os.path.join(HERE, "usage_stats.json")      # 模型用量统计
RELATIONS_FILE = os.path.join(HERE, "relations.json")    # 角色之间的关系
WORLD_FILE = os.path.join(HERE, "world_events.json")     # 村里最近发生的事
PLAYER_FILE = os.path.join(HERE, "player.json")          # 玩家档案（血量/金币/背包）
NPC_STATE_FILE = os.path.join(HERE, "npc_state.json")    # 角色当前血量（战斗后）
BATTLES_FILE = os.path.join(HERE, "battles.json")        # 战斗记录
GROUPS_FILE = os.path.join(HERE, "groups.json")          # 群聊房间（多角色对话）
SPELLS_FILE = os.path.join(HERE, "spells.json")          # 法术目录
AMBIENT_TTL = 600                                        # 天气缓存 10 分钟
_AMBIENT = {"data": None, "ts": 0.0}

VOICE_APP = os.path.join(HERE, "voice_call", "VoiceCall.app")   # 录音助手
VOICE_BIN = "MacOS/VoiceCall --wav"   # 只匹配录音中的实例，别误杀别的进程
_VOICE_LOCK = threading.Lock()          # 同一时间只允许一段录音
_WHISPER = {"model": None, "lock": threading.Lock(), "error": ""}

DEFAULT_PLAYER = {
    "player_name": "旅人",
    "player_hp": 60,
    "gold": 100,
    "inventory": [],
    "quests": {},
}

_PLAYER = {"data": None, "mtime": -1}
ERASED_FILE = os.path.join(HERE, "erased_last.json")  # 回收站：只存最近一次抹除

CLIENT = LLMClient()
SESSIONS = {}                 # "会话id|模式|角色" -> {...}   （内存缓存）
LOCK = threading.Lock()


def load_settings():
    """读设置文件；没有就返回默认值。"""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[设置读取失败] {e}")
    return {"disabled": []}


def save_settings(s):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def disabled_set():
    """被停用或被抹除的角色 key 集合。

    停用 = 只是从列表隐藏，存档保留；
    抹除 = 彻底移除，连同存档、头像一起清掉。
    """
    s = load_settings()
    return set(s.get("disabled", [])) | set(s.get("deleted", []))


AVATAR_SLOTS = ("neutral", "happy", "angry")


# ================================================================ 语音（TTS）
_PLAYING = {"proc": None, "lock": threading.Lock(), "stopped": False}


def _play_bytes(data, register=False):
    """用 afplay 在本机扬声器播放 WAV 字节。

    register=True 时把进程记下来，好让 /call/stop_speak 中途掐掉（打断用）。
    """
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="tts_play_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        proc = subprocess.Popen(["afplay", path],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        if register:
            with _PLAYING["lock"]:
                _PLAYING["proc"] = proc
                _PLAYING["stopped"] = False
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
        finally:
            if register:
                with _PLAYING["lock"]:
                    if _PLAYING["proc"] is proc:
                        _PLAYING["proc"] = None
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _resolve_voice(body):
    """从请求里算出最终要用哪个音色：角色配置优先，然后显式参数，最后全局兜底。"""
    key = body.get("key") or ""
    voice = body.get("voice") or None
    rate = body.get("rate") or None
    pitch = body.get("pitch") or None

    cfg = (NPC_PERSONAS.get(key) or {}).get("voice") or {}
    if isinstance(cfg, dict):
        voice = voice or cfg.get("name")
        rate = rate or cfg.get("rate")
        pitch = pitch or cfg.get("pitch")
    if not voice:
        voice = body.get("fallback_voice") or None
    return voice, rate, pitch


def _say_voices(only_zh=True):
    """列出系统语音。默认只返回中文的（列表太长）。"""
    out = []
    try:
        r = subprocess.run(["say", "-v", "?"], capture_output=True,
                           text=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return out
    for line in r.stdout.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 2:
            continue
        name, lang = parts[0], parts[1]
        sample = parts[2].lstrip("# ").strip() if len(parts) > 2 else ""
        out.append({"name": name, "lang": lang, "sample": sample})
    if only_zh:
        zh = [v for v in out if v["lang"].lower().startswith("zh")]
        return zh or out
    return out


def _tts_wav(text, voice=None, rate=None, pitch=None):
    """用 macOS 的 say + afconvert 生成 WAV 字节。

    pitch：音高偏移（半音），正数更尖、负数更沉（用 say 的 [[pbas N]] 指令）。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("文本是空的")
    text = text[:600]                      # 别让它念太长

    spoken = text
    try:
        if pitch:
            spoken = f"[[pbas {int(pitch):+d}]]" + spoken
    except (TypeError, ValueError):
        pass

    tmp = tempfile.mkdtemp(prefix="tts_")
    try:
        aiff = os.path.join(tmp, "a.aiff")
        wav = os.path.join(tmp, "a.wav")

        cmd = ["say", "-o", aiff]
        if voice:
            cmd += ["-v", voice]
        if rate:
            cmd += ["-r", str(int(rate))]
        cmd.append(spoken)
        subprocess.run(cmd, check=True, timeout=40,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16", aiff, wav],
                       check=True, timeout=40,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        with open(wav, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _looks_like_svg(data):
    head = data[:400].lower()
    return b"<svg" in head


def _store_avatar(key, slot, data):
    """保存上传的头像。

    静图统一转成 PNG；SVG 和动图 GIF 原样保留（GIF 转了就不动了）。
    文件名自动改成 key[-slot].ext —— 省得你记命名规则。
    返回 (相对路径, 扩展名)。
    """
    import io as _io

    is_gif = data[:6] in (b"GIF87a", b"GIF89a")
    ext = "svg" if _looks_like_svg(data) else ("gif" if is_gif else "png")
    base = key if slot == "neutral" else f"{key}-{slot}"
    fname = f"{base}.{ext}"
    os.makedirs(AVATAR_DIR, exist_ok=True)
    path = os.path.join(AVATAR_DIR, fname)

    if ext == "gif":
        # 动图原样存，别转 PNG（转了就不动了）
        with open(path, "wb") as f:
            f.write(data)
    elif ext == "png":
        try:
            from PIL import Image
            img = Image.open(_io.BytesIO(data))
            img.load()
            if img.mode not in ("RGBA", "LA"):
                img = img.convert("RGBA")
            if max(img.size) > 1024:          # 太大就缩一下，省流量
                img.thumbnail((1024, 1024))
            img.save(path, "PNG")
        except Exception as e:
            raise ValueError(f"不是能识别的图片（{e}）")
    else:
        with open(path, "wb") as f:
            f.write(data)

    # 同一个槽位如果还留着别的扩展名的旧文件，清掉，免得两种格式混着
    for other in ("png", "svg", "jpg", "jpeg", "webp", "gif", "bmp"):
        if other != ext:
            stale = os.path.join(AVATAR_DIR, f"{base}.{other}")
            if os.path.exists(stale):
                try:
                    os.remove(stale)
                except OSError:
                    pass
    return f"avatars/{fname}", ext


def _set_character_avatar(key, rel):
    """把 characters.json 里这个角色的 avatar 字段改掉。"""
    for e in personas_mod.CHARACTERS:
        if e.get("key") == key:
            e["avatar"] = rel
            personas_mod.write_raw(personas_mod.CHARACTERS)
            return True
    return False


def reload_personas():
    """角色表改动后重新加载。改的是 JSON 数据，不用重启服务，也不碰任何代码。"""
    global NPC_PERSONAS
    NPC_PERSONAS = personas_mod.load_characters()


_CHAR_MTIME = {"t": 0}


def maybe_reload_personas():
    """发现 characters.json 被改过就自动重载 —— 编辑完不用重启服务。"""
    global NPC_PERSONAS
    try:
        mtime = os.path.getmtime(personas_mod.CHARACTERS_FILE)
    except OSError:
        return
    if mtime != _CHAR_MTIME["t"]:
        _CHAR_MTIME["t"] = mtime
        try:
            NPC_PERSONAS = personas_mod.load_characters()
            print("[角色表] 检测到 characters.json 变化，已自动重载")
        except Exception as e:
            print(f"[角色表] 自动重载失败：{e}")


def load_erased():
    if os.path.exists(ERASED_FILE):
        try:
            with open(ERASED_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_erased(data):
    with open(ERASED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def clear_erased():
    try:
        os.remove(ERASED_FILE)
    except OSError:
        pass


def session_key(session_id, mode, npc_key):
    """一个 NPC（或助理）只有一个存档。

    不再按浏览器会话区分 —— 存档属于「玩家 ↔ 这个角色」这一对关系。
    所以换浏览器、清缓存，进度都还在同一份。
    """
    return f"{mode}|{npc_key if mode == 'npc' else '-'}"


def parse_key(key):
    """从存档 key 解析出 (mode, npc)。兼容旧的「会话|模式|角色」格式。"""
    parts = [p for p in str(key).split("|") if p != ""]
    if len(parts) >= 3:
        return parts[-2], parts[-1]        # 旧格式：取最后两段
    if len(parts) == 2:
        return parts[0], parts[1]
    return "assistant", "-"


def _save_path(key):
    """给会话键起一个安全的文件名。"""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return os.path.join(SAVE_DIR, safe + ".json")


def save_session(key, sess):
    """把对话历史和游戏状态写到本地。"""
    try:
        payload = {
            "key": key,
            "mode": sess.get("mode"),
            "messages": sess["agent"].history,
            "state": sess.get("state"),
            "mood": sess.get("mood", "neutral"),
            "summary": sess["agent"].summary,
            "summarized_upto": sess["agent"].summarized_upto,
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
        with open(_save_path(key), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"[存档失败] {key}: {e}")


def load_saved(key):
    """读回本地存档；没有就返回 None。"""
    path = _save_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[读档失败] {key}: {e}")
        return None


MIME = {
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".html": "text/html; charset=utf-8", ".css": "text/css",
    ".js": "application/javascript", ".json": "application/json",
    ".ico": "image/x-icon",
}


def _iter_saves():
    """遍历存档目录，产出 (文件名, 解析后的内容)。坏的跳过。"""
    if not os.path.isdir(SAVE_DIR):
        return
    for fn in sorted(os.listdir(SAVE_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SAVE_DIR, fn), encoding="utf-8") as f:
                yield fn, json.load(f)
        except Exception:
            continue


def list_saves():
    """列出所有存档，带上角色名、回合数、更新时间。"""
    out = []
    for fn, d in _iter_saves():
        key = d.get("key") or fn[:-5]
        mode, npc_key = parse_key(key)
        npc = get_npc(npc_key)
        if npc:
            name, avatar = npc["name"], npc.get("avatar", "")
        elif mode == "assistant":
            name, avatar = ASSISTANT_NAME, ASSISTANT_AVATAR
        else:
            name, avatar = npc_key, ""
        msgs = d.get("messages", [])
        out.append({
            "key": key, "file": fn, "mode": mode, "npc": npc_key,
            "name": name, "avatar": avatar,
            "turns": sum(1 for m in msgs if m.get("role") == "user"),
            "updated": d.get("updated"),
            "state": d.get("state"),
        })
    out.sort(key=lambda x: (x["mode"] != "assistant", x["name"]))
    return out


def migrate_saves():
    """把旧格式（会话|模式|角色）合并成新格式（模式|角色），每个角色只留最新的一份。"""
    if not os.path.isdir(SAVE_DIR):
        return 0
    newest = {}
    old_files = []
    for fn, d in _iter_saves():
        key = d.get("key") or fn[:-5]
        if len([p for p in key.split("|") if p]) >= 3:      # 旧格式
            old_files.append(fn)
            new_key = "|".join([p for p in key.split("|") if p][-2:])
            cur = newest.get(new_key)
            if cur is None or (d.get("updated") or "") > (cur.get("updated") or ""):
                newest[new_key] = d
    for fn in old_files:
        try:
            os.remove(os.path.join(SAVE_DIR, fn))
        except OSError:
            pass
    for new_key, d in newest.items():
        d["key"] = new_key
        with open(_save_path(new_key), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    return len(newest)


def export_bundle(session_id=None):
    """把所有存档打包成一个可导出的对象（每个角色一份）。"""
    saves = [d for _, d in _iter_saves()]
    saves.sort(key=lambda d: str(d.get("key", "")))
    return {"format": "assistant_agent_saves", "version": 2,
            "exported": datetime.now().isoformat(timespec="seconds"),
            "saves": saves}


def _load_model_keys():
    """角色专属的 API key（单独存，不进 characters.json，也不进备份）。"""
    if os.path.exists(MODEL_KEYS_FILE):
        try:
            with open(MODEL_KEYS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def _save_model_keys(d):
    with open(MODEL_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _load_relations():
    """角色关系表：{角色: {另一个角色: 关系描述}}"""
    if os.path.exists(RELATIONS_FILE):
        try:
            with open(RELATIONS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return {k: v for k, v in d.items()
                    if isinstance(v, dict) and not k.startswith("_")}
        except Exception:
            pass
    return {}


def _recent_events(limit=5):
    """村里最近发生的事（越新越后）。"""
    if os.path.exists(WORLD_FILE):
        try:
            with open(WORLD_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list):
                return [e.get("text", "") for e in d[-limit:] if e.get("text")]
        except Exception:
            pass
    return []


def _append_event(text, kind=None):
    """记一条世界事件（最多留 50 条）。kind="announcement" 表示村公告。"""
    if not text:
        return
    with LOCK:
        d = []
        if os.path.exists(WORLD_FILE):
            try:
                with open(WORLD_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, list):
                    d = []
            except Exception:
                d = []
        item = {"t": datetime.now().isoformat(timespec="seconds"),
                "text": text}
        if kind:
            item["kind"] = kind
        d.append(item)
        with open(WORLD_FILE, "w", encoding="utf-8") as f:
            json.dump(d[-50:], f, ensure_ascii=False, indent=1)




def _latest_announcement(hours=24):
    """最近 24 小时内最新的一条村公告（没有就返回空串）。"""
    if not os.path.exists(WORLD_FILE):
        return ""
    try:
        with open(WORLD_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return ""
    now = datetime.now()
    for e in reversed(d if isinstance(d, list) else []):
        if not isinstance(e, dict) or e.get("kind") != "announcement":
            continue
        try:
            t = datetime.fromisoformat(e.get("t", ""))
        except (TypeError, ValueError):
            t = now
        if (now - t).total_seconds() <= hours * 3600:
            return (e.get("text") or "").strip()
        break
    return ""


def _event_from(npc, changes):
    """把一次交易变成一句"村里传开的事"。"""
    items = [c["item"] for c in (changes or []) if c["kind"] == "item_add"]
    healed = any(c["kind"] == "player_hp" and c.get("delta", 0) > 0
                 for c in (changes or []))
    parts = []
    if items:
        parts.append(f"旅人在{npc.get('name','某人')}那儿买了{'、'.join(items)}")
    if healed:
        parts.append(f"旅人在{npc.get('name','某人')}那儿歇过脚、缓过劲来")
    return "；".join(parts) or None


def _seed_player():
    """第一次运行时，从最近的存档里捞一份状态当种子。"""
    seed = dict(DEFAULT_PLAYER)
    best_updated, best_state = "", None
    for _, d in _iter_saves():
        st = d.get("state")
        if isinstance(st, dict) and st:
            if (d.get("updated") or "") > best_updated:
                best_updated = d.get("updated") or ""
                best_state = st
    if best_state:
        for k in ("player_name", "player_hp", "gold", "inventory", "quests"):
            if k in best_state:
                seed[k] = best_state[k]
    return seed


def _read_player_file():
    if os.path.exists(PLAYER_FILE):
        try:
            with open(PLAYER_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                out = dict(DEFAULT_PLAYER)
                out.update(d)
                return out
        except Exception:
            pass
    seed = _seed_player()
    save_player(seed)
    return seed


def get_player():
    """全局唯一的玩家档案（所有角色共享同一份）。"""
    try:
        mtime = os.path.getmtime(PLAYER_FILE)
    except OSError:
        mtime = -1
    if _PLAYER["data"] is None or mtime != _PLAYER["mtime"]:
        _PLAYER["data"] = _read_player_file()
        try:
            _PLAYER["mtime"] = os.path.getmtime(PLAYER_FILE)
        except OSError:
            _PLAYER["mtime"] = -1
    return _PLAYER["data"]


def save_player(data):
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    with open(PLAYER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        _PLAYER["mtime"] = os.path.getmtime(PLAYER_FILE)
    except OSError:
        pass


# ================================================================ 时间 / 天气
#
# 主界面顶上那条「几点 / 几号 / 什么天气」。
# 时间用本机时钟（前端每秒钟自己跳），天气走 Open-Meteo（免 key），
# 位置优先读 settings.json 的 ambient，其次按公网 IP 猜，最后落到兰州。

WMO_TEXT = {
    0: ("晴", "☀️"), 1: ("少云", "🌤️"), 2: ("多云", "⛅"), 3: ("阴", "☁️"),
    45: ("有雾", "🌫️"), 48: ("雾凇", "🌫️"),
    51: ("毛毛雨", "🌦️"), 53: ("小雨", "🌦️"), 55: ("中雨", "🌦️"),
    56: ("冻雨", "🌧️"), 57: ("冻雨", "🌧️"),
    61: ("小雨", "🌧️"), 63: ("中雨", "🌧️"), 65: ("大雨", "🌧️"),
    66: ("冻雨", "🌧️"), 67: ("冻雨", "🌧️"),
    71: ("小雪", "🌨️"), 73: ("中雪", "🌨️"), 75: ("大雪", "🌨️"),
    77: ("雪粒", "🌨️"),
    80: ("阵雨", "🌦️"), 81: ("阵雨", "🌧️"), 82: ("强阵雨", "🌧️"),
    85: ("阵雪", "🌨️"), 86: ("强阵雪", "🌨️"),
    95: ("雷阵雨", "⛈️"), 96: ("雷暴冰雹", "⛈️"), 99: ("雷暴冰雹", "⛈️"),
}

CITY_ZH = {
    "Lanzhou": "兰州", "Beijing": "北京", "Shanghai": "上海",
    "Guangzhou": "广州", "Shenzhen": "深圳", "Chengdu": "成都",
    "Xian": "西安", "Xi'an": "西安", "Hangzhou": "杭州",
    "Nanjing": "南京", "Wuhan": "武汉", "Chongqing": "重庆",
    "Tianjin": "天津", "Suzhou": "苏州", "Zhengzhou": "郑州",
}


def _ambient_cfg():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        a = d.get("ambient") if isinstance(d, dict) else None
        return a if isinstance(a, dict) else {}
    except Exception:
        return {}


def _ambient_location():
    cfg = _ambient_cfg()
    if cfg.get("lat") is not None and cfg.get("lon") is not None:
        return {"city": cfg.get("city") or "本地",
                "lat": float(cfg["lat"]), "lon": float(cfg["lon"]),
                "source": "settings"}
    try:
        d = requests.get("https://ipinfo.io/json", timeout=5).json()
        loc = (d.get("loc") or "").split(",")
        if len(loc) == 2:
            city = d.get("city") or "本地"
            return {"city": CITY_ZH.get(city, city),
                    "lat": float(loc[0]), "lon": float(loc[1]),
                    "source": "ip"}
    except Exception:
        pass
    return {"city": cfg.get("city") or "兰州",
            "lat": 36.0611, "lon": 103.8343, "source": "default"}


def _daypart(h):
    if h < 5:
        return "凌晨"
    if h < 9:
        return "早上"
    if h < 12:
        return "上午"
    if h < 14:
        return "中午"
    if h < 18:
        return "下午"
    if h < 21:
        return "傍晚"
    return "夜里"


def _weather_feel(t):
    if t <= 0:
        return "很冷"
    if t <= 8:
        return "冷"
    if t <= 14:
        return "偏冷"
    if t <= 22:
        return "舒服"
    if t <= 28:
        return "偏热"
    return "热"


def _wind_word(v):
    if v < 5:
        return "没什么风"
    if v < 20:
        return "风不大"
    if v < 40:
        return "风有点冲"
    return "风很大"


def _ambient_brief(with_city=False):
    """给模型看的一句「此刻」：不联网，只读缓存，随叫随到。"""
    d = _AMBIENT["data"]
    dt = datetime.now()
    part = _daypart(dt.hour)
    s = (f"现在是 {dt.month}月{dt.day}日 "
         f"周{'一二三四五六日'[dt.weekday()]} {part} "
         f"{dt.hour:02d}:{dt.minute:02d}")
    w = (d or {}).get("weather")
    if w:
        city = ((d or {}).get("loc") or {}).get("city") if with_city else ""
        where = f"{city} " if city else "外面"
        s += (f"；{where}{w.get('text')}，{w.get('temp')}°C"
              f"（{_weather_feel(float(w.get('temp') or 0))}），"
              f"{_wind_word(float(w.get('wind') or 0))}，"
              f"湿度 {w.get('humidity')}%")
        if not w.get("is_day"):
            s += "，天已经黑了"
    return s + "。"


def _ambient_hint():
    """场面话，不是角色规则：只描述「外头是什么状况」，怎么应对交给角色自己。"""
    d = _AMBIENT["data"] or {}
    w = d.get("weather") or {}
    code = int(w.get("code") or 0)
    out = []
    if 95 <= code <= 99:
        out.append("外头在打雷")
    elif 71 <= code <= 77 or 85 <= code <= 86:
        out.append("外头在下雪")
    elif 51 <= code <= 67 or 80 <= code <= 82:
        out.append("外头下着雨，没人愿意在外面久站")
    dt = datetime.now()
    if w.get("is_day") == 0 or dt.hour >= 22 or dt.hour < 5:
        out.append("夜里了，街上没什么人")
    elif 6 <= dt.hour < 9:
        out.append("刚开张的点，街上人还少")
    return "；".join(out) + "。" if out else ""


def _assistant_context():
    """小助的实时状态：时间天气 + 魔力 + 一句使用说明。"""
    magic = _magic_text()
    ann = _latest_announcement()
    return (_ambient_brief(with_city=True) + "\n"
            + (magic + "\n" if magic else "")
            + (f"【村公告】{ann}\n" if ann else "")
            + "（这是本机所在地的真实时间与天气，回答这类问题优先用它，"
              "别自己猜城市；要别的城市再用 get_weather。）")


def _local_weather_text():
    """get_weather 不传城市时返回什么；没缓存就现拉一次。"""
    if not (_AMBIENT["data"] or {}).get("weather"):
        try:
            _ambient_payload()
        except Exception:
            pass
    return _ambient_brief(with_city=True)


set_local_weather(_local_weather_text)


def _ambient_loop():
    """后台每 10 分钟刷一次天气，聊天时就不用等网络。"""
    while True:
        try:
            _ambient_payload(force=True)
        except Exception:
            pass
        time.sleep(AMBIENT_TTL)



MAGIC_LEVELS = ((85, "澎湃"), (65, "充沛"), (45, "平稳"),
                (25, "微弱"), (0, "枯竭"))


def _magic(w):
    """魔力感应（0~100）：由真实天气 + 湿度 + 温度 + 风 + 时辰共同决定。

    不是随机数 —— 打雷、起雾、夜里会涨；正午、暴晒、狂躁的风会压。
    值越高，法术强度越大、能放的法术也越多。
    """
    w = w or {}
    try:
        hum = float(w.get("humidity") or 50)
        temp = float(w.get("temp") or 15)
        wind = float(w.get("wind") or 0)
        code = int(w.get("code") or 0)
    except (TypeError, ValueError):
        hum, temp, wind, code = 50, 15, 0, 0
    hour = datetime.now().hour

    score = 45.0

    # 天气：雷暴最通灵，其次雾、雪、雨、阴
    if 95 <= code <= 99:
        score += 28
    elif code in (45, 48):
        score += 18
    elif 71 <= code <= 77 or 85 <= code <= 86:
        score += 14
    elif 51 <= code <= 67 or 80 <= code <= 82:
        score += 12
    elif code == 3:
        score += 6

    # 湿度：越潮越"通灵"
    score += (hum - 50) * 0.18

    # 温度：10~24 最合适；太冷太热都碍事
    if 10 <= temp <= 24:
        score += 6
    elif temp < 0:
        score -= 8
    elif temp < 5:
        score -= 4
    elif temp > 32:
        score -= 7
    elif temp > 28:
        score -= 3

    # 风：微风助势，狂风扰乱
    if 10 <= wind <= 30:
        score += 4
    elif wind > 45:
        score -= 6

    # 时辰：子时最盛，正午最弱
    score += {0: 8, 1: 9, 2: 8, 3: 6, 4: 4, 5: 2, 6: 0, 7: -1, 8: -2,
              9: -3, 10: -4, 11: -5, 12: -6, 13: -5, 14: -4, 15: -3,
              16: -2, 17: -1, 18: 0, 19: 2, 20: 3, 21: 5, 22: 6,
              23: 7}.get(hour, 0)

    sense = max(0, min(100, round(score)))
    level = next(name for lo, name in MAGIC_LEVELS if sense >= lo)
    return {"sense": sense, "level": level,
            "power": round(1 + sense / 100 * 1.5, 2),   # 法术强度 ×1.0 ~ ×2.5
            "slots": 1 + sense // 25}                   # 可放法术 1 ~ 5 个


def _magic_text():
    m = (_AMBIENT["data"] or {}).get("magic")
    if not m:
        return ""
    return (f"魔力感应 {m['sense']}/100（{m['level']}），"
            f"法术强度 ×{m['power']}，此刻可放 {m['slots']} 个法术")

def _ambient_payload(force=False):
    now = time.time()
    cached = _AMBIENT["data"]
    if not force and cached and now - _AMBIENT["ts"] < AMBIENT_TTL:
        return cached

    dt = datetime.now()
    data = {
        "ok": True,
        "now": dt.isoformat(timespec="seconds"),
        "date": f"{dt.month}月{dt.day}日",
        "weekday": "周" + "一二三四五六日"[dt.weekday()],
        "daypart": _daypart(dt.hour),
        "loc": _ambient_location(),
        "weather": None,
    }

    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": data["loc"]["lat"],
                "longitude": data["loc"]["lon"],
                "current": ("temperature_2m,apparent_temperature,"
                            "relative_humidity_2m,weather_code,"
                            "wind_speed_10m,is_day"),
                "timezone": "auto",
            }, timeout=6)
        cur = (r.json() or {}).get("current") or {}
        code = int(cur.get("weather_code") or 0)
        text, icon = WMO_TEXT.get(code, ("未知", "🌡️"))
        is_day = int(cur.get("is_day") or 0)
        if not is_day and code in (0, 1):
            icon = "🌙"
        data["weather"] = {
            "temp": round(float(cur.get("temperature_2m") or 0), 1),
            "feels": round(float(cur.get("apparent_temperature") or 0), 1),
            "humidity": int(cur.get("relative_humidity_2m") or 0),
            "wind": round(float(cur.get("wind_speed_10m") or 0), 1),
            "code": code, "text": text, "icon": icon, "is_day": is_day,
        }
    except Exception as e:
        data["weather_error"] = f"{type(e).__name__}: {e}"

    data["magic"] = _magic(data.get("weather"))

    # 抓失败就先用上一次的天气顶着，别在界面上闪「不可用」
    if not data["weather"] and cached and cached.get("weather"):
        data["weather"] = cached["weather"]
        data["stale"] = True

    _AMBIENT["data"] = data
    _AMBIENT["ts"] = now if data["weather"] and not data.get("stale") \
        else now - AMBIENT_TTL + 60          # 失败 1 分钟后再试
    return data


# ================================================================ 语音通话
#
# 链路：浏览器点「通话」→ 服务器让 VoiceCall.app 从本机麦克风录音（自动断句）
#      → faster-whisper 转文字 → 走正常 /chat → say 合成 → 本机扬声器播放
#      → 播完再听，循环到挂断。
#
# 为什么录音要在服务器：内嵌预览浏览器没有麦克风权限，而且这样手机/别的设备
# 连过来也能用同一套。

def _whisper_model():
    """懒加载本地 whisper；第一次会下载模型（约 140MB~460MB）。"""
    with _WHISPER["lock"]:
        if _WHISPER["model"] is not None:
            return _WHISPER["model"]
        if _WHISPER["error"]:
            return None
        try:
            from faster_whisper import WhisperModel
            size = os.getenv("WHISPER_MODEL", "small")
            print(f"[语音] 加载 whisper「{size}」"
                  f"（模型源：{os.getenv('HF_ENDPOINT') or 'HuggingFace'}）", flush=True)
            kw = dict(device="cpu",
                      compute_type=os.getenv("WHISPER_COMPUTE", "int8"))
            try:
                # 已经下过就直接读本地，别再去网上问“有没有新版本”（那一步能卡 1 分钟）
                _WHISPER["model"] = WhisperModel(size, local_files_only=True, **kw)
            except Exception:
                print("[语音] 本地没有模型，开始下载（只此一次）…", flush=True)
                _WHISPER["model"] = WhisperModel(size, **kw)
            print(f"[语音] whisper 模型已加载：{size}")
        except Exception as e:
            _WHISPER["error"] = f"{type(e).__name__}: {e}"
            print(f"[语音] 加载失败：{_WHISPER['error']}")
            return None
        return _WHISPER["model"]


def _asr_hotwords():
    """给识别引擎一点领域词，省得把「炉子」听成「炉灯火」。"""
    names = " ".join(n["name"] for n in NPC_PERSONAS.values())
    return (f"{names} 老铁 打铁 铁砧 炉子 煤炭 客栈 住店 酒 山里 猎人 箭 "
            "旅人 金币 生命值 铁剑 村口 收摊")


def _transcribe(path):
    model = _whisper_model()
    if model is None:
        raise RuntimeError(
            _WHISPER["error"] or "没装 faster-whisper，运行：pip install faster-whisper")
    try:
        segments, _info = model.transcribe(
            path, language="zh", beam_size=5,
            vad_filter=True,                               # 静音/噪声直接丢掉
            vad_parameters={"min_silence_duration_ms": 300},
            initial_prompt="以下是普通话的日常对话。",
            hotwords=_asr_hotwords(),
            condition_on_previous_text=False)
        text = "".join(s.text for s in segments).strip()
    except Exception:
        # VAD 模型下不下来时退回普通模式，别让整个通话挂掉
        segments, _info = model.transcribe(
            path, language="zh", beam_size=1,
            condition_on_previous_text=False)
        text = "".join(s.text for s in segments).strip()

    # Whisper 中文常出繁体，统一转简体
    try:
        import zhconv
        text = zhconv.convert(text, "zh-cn")
    except Exception:
        pass
    return text if len(text) >= 2 else ""


def stop_voice_app():
    """杀掉所有残留的录音进程（卡住的、上次没退的）。"""
    try:
        subprocess.run(["pkill", "-f", VOICE_BIN], check=False, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def voice_listen(max_sec=12, silence=1.0):
    """录一句并转成文字；返回 {"text","status","ms"}。"""
    if not os.path.isdir(VOICE_APP):
        raise RuntimeError("缺少 voice_call/VoiceCall.app，先跑 voice_call/build.sh")
    stamp = f"{int(time.time() * 1000)}_{random.randint(100, 999)}"
    wav = os.path.join(tempfile.gettempdir(), f"vc_{stamp}.wav")
    t0 = time.time()
    with _VOICE_LOCK:
        stop_voice_app()                 # 先把上一次可能卡住的清掉
        try:
            subprocess.run(
                ["open", "-W", "-n", VOICE_APP, "--args",
                 "--wav", wav, "--max", str(int(max_sec)),
                 "--silence", str(float(silence))],
                check=False, timeout=max_sec + 15,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            stop_voice_app()
            return {"text": "", "status": "timeout",
                    "ms": int((time.time() - t0) * 1000)}
        finally:
            stop_voice_app()             # 正常退出时是空操作；卡住时强制收尾

    if not os.path.exists(wav) or os.path.getsize(wav) < 2048:
        return {"text": "", "status": "no-audio",
                "ms": int((time.time() - t0) * 1000)}
    try:
        text = _transcribe(wav)
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
    return {"text": text,
            "status": "ok" if text else "empty",
            "ms": int((time.time() - t0) * 1000)}






# ================================================================ 法术
#
# 模型只负责「说出来」，扣法术位、算伤害、改血量全在代码里。
# 数值跟魔力感应走：感应当前值决定法术强度和法术位上限。

SLOT_REGEN_SEC = 600          # 每 10 分钟回 1 个法术位


def _load_spells():
    try:
        with open(SPELLS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _npc_spell_list(npc):
    cat = _load_spells()
    out = []
    for k in (npc.get("spells") or []):
        s = cat.get(k)
        if s:
            out.append({"key": k, "name": s.get("name", k),
                        "cost": int(s.get("cost") or 1),
                        "effect": s.get("effect") or "utility",
                        "desc": s.get("desc", "")})
    return out


def _slot_max():
    m = (_AMBIENT["data"] or {}).get("magic") or {}
    sense = int(m.get("sense") or 50)
    return max(1, min(5, 1 + sense // 25))


def _slots(key, persist=False):
    """返回 (剩几个, 上限)。按时间自动回，最多回到上限。"""
    mx = _slot_max()
    st = _load_npc_state().get(key) or {}
    now = time.time()
    try:
        ts = float(st.get("slots_ts") or now)
    except (TypeError, ValueError):
        ts = now
    left = st.get("slots_left")
    left = mx if left is None else int(left)

    gained = int((now - ts) // SLOT_REGEN_SEC)
    if gained > 0:
        left = min(mx, left + gained)
        ts += gained * SLOT_REGEN_SEC
        if persist:
            with LOCK:
                d = _load_npc_state()
                row = d.get(key) or {}
                row["slots_left"] = left
                row["slots_ts"] = ts
                d[key] = row
                _save_npc_state(d)

    return max(0, min(mx, left)), mx


def _cast_spell(npc_key, npc, state, mood_holder, args):
    """执行一次施法；返回给模型看的 JSON（它据此编台词）。"""
    print(f"[法术] {npc_key} 触发 cast_spell: {args}", flush=True)
    cat = _load_spells()
    skey = (args.get("spell") or "").strip()
    target = (args.get("target") or "player").strip()

    # 模型可能传角色名（猎人）而不是 key（hunter），统一转成 key
    if target and target not in ("player", "玩家", "我", ""):
        if target not in NPC_PERSONAS:
            for k, v in NPC_PERSONAS.items():
                if v.get("name") == target:
                    target = k
                    break
            else:
                target = "player"
    sp = cat.get(skey)

    if not sp or skey not in (npc.get("spells") or []):
        return json.dumps({"ok": False,
                           "reason": f"{npc['name']}不会「{skey}」这个法术"},
                          ensure_ascii=False)

    # 一次对话里只允许放一次，防止模型上头连发
    if mood_holder is not None and mood_holder.get("casts", 0) >= 1:
        return json.dumps({"ok": False, "reason": "这一轮已经施过法了，先歇口气"},
                          ensure_ascii=False)

    left, mx = _slots(npc_key)
    cost = int(sp.get("cost") or 1)
    if left < cost:
        return json.dumps({"ok": False,
                           "reason": f"法术位不够（剩 {left}，需要 {cost}），"
                                     "得等一会儿才能再放"},
                          ensure_ascii=False)

    magic = (_AMBIENT["data"] or {}).get("magic") or {}
    power = float(magic.get("power") or 1.0)
    effect = sp.get("effect") or "utility"
    base = float(sp.get("base") or 0)
    roll = 0
    if effect in ("damage", "heal"):
        roll = max(1, int(round(base * power * random.uniform(0.9, 1.1))))

    hp_before = hp_after = None
    to_player = target in ("", "player", "玩家") or target not in NPC_PERSONAS

    if effect == "damage":
        if to_player:
            hp_before = int(state.get("player_hp", 100))
            hp_after = max(1, hp_before - roll)      # 打服不打杀
            state["player_hp"] = hp_after
            save_player(state)
            target = "player"
        else:
            tn = NPC_PERSONAS[target]
            hp_before, tn_max = _cur_hp(target, tn)
            hp_after = max(1, hp_before - roll)
            _set_npc_hp(target, hp_after)
    elif effect == "heal":
        if to_player:
            hp_before = int(state.get("player_hp", 100))
            hp_after = min(100, hp_before + roll)
            state["player_hp"] = hp_after
            save_player(state)
            target = "player"
        else:
            tn = NPC_PERSONAS[target]
            hp_before, tn_max = _cur_hp(target, tn)
            hp_after = min(tn_max, hp_before + roll)
            _set_npc_hp(target, hp_after)

    # 扣位
    with LOCK:
        d = _load_npc_state()
        row = d.get(npc_key) or {}
        row["slots_left"] = max(0, left - cost)
        row["slots_ts"] = row.get("slots_ts") or time.time()
        row["last_cast"] = datetime.now().isoformat(timespec="seconds")
        d[npc_key] = row
        _save_npc_state(d)

    if mood_holder is not None:
        mood_holder["casts"] = mood_holder.get("casts", 0) + 1

    return json.dumps({
        "ok": True, "spell": sp.get("name"), "effect": effect,
        "target": target, "power": power, "roll": roll,
        "hp_before": hp_before, "hp_after": hp_after,
        "slots_left": max(0, left - cost), "slots_max": mx,
        "desc": sp.get("desc", ""),
    }, ensure_ascii=False)

# ================================================================ 战斗引擎
def _npc_stats(npc):
    s = (npc or {}).get("stats") or {}
    return {
        "hp": int(s.get("hp") or 80),
        "atk": int(s.get("atk") or 10),
        "def": int(s.get("def") or 5),
        "spd": int(s.get("spd") or 6),
    }


def _load_npc_state():
    if os.path.exists(NPC_STATE_FILE):
        try:
            with open(NPC_STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return {}


def _save_npc_state(d):
    with open(NPC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _cur_hp(key, npc):
    mx = _npc_stats(npc)["hp"]
    st = _load_npc_state().get(key) or {}
    v = st.get("hp")
    return (mx if v is None else max(0, min(mx, int(v)))), mx


def _set_npc_hp(key, hp):
    with LOCK:
        d = _load_npc_state()
        row = d.get(key) or {}
        row["hp"] = max(0, int(hp))
        row["updated"] = datetime.now().isoformat(timespec="seconds")
        d[key] = row
        _save_npc_state(d)


def _run_duel(a_key, b_key):
    """算一场真战斗：谁先手、打多少、谁赢，全部在 Python 里定。"""
    a, b = NPC_PERSONAS[a_key], NPC_PERSONAS[b_key]
    maxhp = {a_key: _npc_stats(a)["hp"], b_key: _npc_stats(b)["hp"]}
    hp = {a_key: _cur_hp(a_key, a)[0], b_key: _cur_hp(b_key, b)[0]}
    rounds = []

    for r in range(1, 13):
        # 速度决定先手，带一点随机
        order = sorted(
            [a_key, b_key],
            key=lambda k: _npc_stats(NPC_PERSONAS[k])["spd"]
            + random.uniform(0, 5),
            reverse=True)
        for actor in order:
            target = b_key if actor == a_key else a_key
            if hp[target] <= 0:
                continue
            atk = _npc_stats(NPC_PERSONAS[actor])["atk"]
            dfn = _npc_stats(NPC_PERSONAS[target])["def"]
            dmg = max(1, int(round(atk - dfn * 0.45
                                   + random.randint(-2, 3))))
            crit = random.random() < 0.12
            if crit:
                dmg = int(round(dmg * 1.6))
            hp[target] = max(0, hp[target] - dmg)
            rounds.append({
                "round": r, "actor": actor, "target": target,
                "damage": dmg, "crit": crit,
                "a_hp": hp[a_key], "b_hp": hp[b_key],
            })
            if hp[target] <= 0:
                break
        if hp[a_key] <= 0 or hp[b_key] <= 0:
            break

    if hp[a_key] <= 0 and hp[b_key] <= 0:
        winner = None
    elif hp[b_key] <= 0:
        winner = a_key
    elif hp[a_key] <= 0:
        winner = b_key
    else:
        ra, rb = hp[a_key] / maxhp[a_key], hp[b_key] / maxhp[b_key]
        winner = (a_key if ra > rb + 0.05
                  else b_key if rb > ra + 0.05 else None)

    # 落盘：倒下的留 1 点血（没死，只是被打服了）
    for k in (a_key, b_key):
        if hp[k] <= 0:
            hp[k] = 1
        _set_npc_hp(k, hp[k])

    return {"rounds": rounds, "winner": winner, "hp": hp, "maxhp": maxhp}


def _duel_transcript(res, a, b, a_key, b_key):
    """把算好的回合转成给模型看的战报。"""
    lines = []
    for r in res["rounds"]:
        anc = a["name"] if r["actor"] == a_key else b["name"]
        tnc = b["name"] if r["target"] == b_key else a["name"]
        crit = "（打中要害）" if r["crit"] else ""
        lines.append(
            f"第{r['round']}回合：{anc} 打中 {tnc}，伤害 {r['damage']}{crit}。"
            f"此时 {a['name']} {r['a_hp']}/{res['maxhp'][a_key]}，"
            f"{b['name']} {r['b_hp']}/{res['maxhp'][b_key]}。")
    return "\n".join(lines)


def _log_battle(rec):
    with LOCK:
        d = []
        if os.path.exists(BATTLES_FILE):
            try:
                with open(BATTLES_FILE, encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, list):
                    d = []
            except Exception:
                d = []
        d.append(rec)
        with open(BATTLES_FILE, "w", encoding="utf-8") as f:
            json.dump(d[-100:], f, ensure_ascii=False, indent=1)


# ================================================================ 外观（主题 / 背景）

def _list_bg():
    out = []
    if os.path.isdir(BG_DIR):
        for n in sorted(os.listdir(BG_DIR), reverse=True):
            if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                out.append({"name": n, "url": f"bg/{n}"})
    return out


def _appearance():
    s = load_settings() or {}
    a = s.get("appearance") or {}
    theme = a.get("theme") if a.get("theme") in APPEARANCE_THEMES else "night"
    bg = (a.get("bg") or "").strip()
    if bg and not os.path.exists(os.path.join(WEB_DIR, bg)):
        bg = ""                                   # 文件被删了就当没设
    try:
        dim = max(0.0, min(0.95, float(a.get("dim", 0.55))))
    except (TypeError, ValueError):
        dim = 0.55
    try:
        blur = max(0.0, min(24.0, float(a.get("blur", 0))))
    except (TypeError, ValueError):
        blur = 0.0
    try:
        gblur = max(0.0, min(30.0, float(a.get("glass_blur", 16))))
    except (TypeError, ValueError):
        gblur = 16.0
    return {"theme": theme, "bg": bg, "dim": dim, "blur": blur,
            "glass": bool(a.get("glass", True)),
            "glass_blur": gblur,
            "bg_list": _list_bg()}


def _save_appearance(body):
    theme = body.get("theme") if body.get("theme") in APPEARANCE_THEMES else "night"
    bg = (body.get("bg") or "").strip()
    if bg and not os.path.exists(os.path.join(WEB_DIR, bg)):
        bg = ""
    try:
        dim = max(0.0, min(0.95, float(body.get("dim", 0.55))))
    except (TypeError, ValueError):
        dim = 0.55
    try:
        blur = max(0.0, min(24.0, float(body.get("blur", 0))))
    except (TypeError, ValueError):
        blur = 0.0
    try:
        gblur = max(0.0, min(30.0, float(body.get("glass_blur", 16))))
    except (TypeError, ValueError):
        gblur = 16.0
    glass = body.get("glass", True)
    if isinstance(glass, str):
        glass = glass.lower() not in ("0", "false", "off", "no")
    with LOCK:
        s = load_settings() or {}
        s["appearance"] = {"theme": theme, "bg": bg, "dim": dim, "blur": blur,
                           "glass": bool(glass), "glass_blur": gblur}
        save_settings(s)
    return _appearance()


def _store_bg(data):
    limit = lambda mb: mb * 1024 * 1024
    if data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        ext = "gif"
        if len(data) > limit(30):
            raise ValueError("动图太大了（GIF 上限 30MB）")
    else:
        raise ValueError("只支持 jpg / png / webp / gif")
    if ext != "gif" and len(data) > limit(12):
        raise ValueError("图片太大了（上限 12MB）")
    os.makedirs(BG_DIR, exist_ok=True)
    name = f"bg_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
    with open(os.path.join(BG_DIR, name), "wb") as f:
        f.write(data)
    return f"bg/{name}"


# ================================================================ 群聊
#
# 和对戏的区别：
#   对戏 = 一次模型调用，让它写完两个人的台词（省 token，但只有一个大脑）
#   群聊 = 每个角色各自一次调用，只写自己那句（贵一点，但每个角色真正独立）
#
# 角色之间通过「共享对话记录」交流：轮到谁，谁就看到前面所有人说的话。

def _load_groups():
    if os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return {}


def _save_groups(d):
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _group_members(g):
    return [k for k in g.get("members", []) if k in NPC_PERSONAS]


def _group_public(g):
    """给前端的房间数据（补上头像和显示名）。"""
    members = []
    for k in _group_members(g):
        n = NPC_PERSONAS[k]
        members.append({"key": k, "name": n["name"],
                        "avatar": n.get("avatar", "")})
    return {
        "id": g["id"],
        "name": g.get("name") or "群聊",
        "topic": g.get("topic", ""),
        "members": members,
        "lines": g.get("lines", []),
        "created": g.get("created", ""),
    }


def _group_summary(g):
    lines = g.get("lines") or []
    last = lines[-1] if lines else None
    return {
        "id": g["id"],
        "name": g.get("name") or "群聊",
        "topic": g.get("topic", ""),
        "members": [{"key": k, "name": NPC_PERSONAS[k]["name"]}
                    for k in _group_members(g)],
        "count": len(lines),
        "created": g.get("created", ""),
        "last": ({"speaker": last.get("speaker"), "text": last.get("text", "")[:40]}
                 if last else None),
    }


def _group_names(g):
    return "、".join(NPC_PERSONAS[k]["name"] for k in _group_members(g))


def _group_system(npc):
    return (npc["prompt"] + "\n\n【当前场合】\n"
            "这不是一对一，是几个人凑在一块儿的场合。\n"
            "你只说你自己的一两句话（30~70 字），像真人随口接话。\n"
            "不要替别人说话，不要写旁白，不要在开头写你自己的名字。\n"
            "可以说得半截、可以被打断、可以只哼一声，别每句都完整漂亮。\n"
            "这次不需要调用任何工具，也别提工具、系统、规则这些东西。")


def _group_user(g, spk_key):
    spk = NPC_PERSONAS[spk_key]
    pname = get_player().get("player_name") or "旅人"
    rel = _load_relations().get(spk_key) or {}

    rel_lines = []
    for k in _group_members(g):
        if k == spk_key:
            continue
        rel_lines.append(f"- {NPC_PERSONAS[k]['name']}："
                         f"{rel.get(k) or '不太熟'}")

    recent = "；".join(_recent_events(5)) or "（没什么新鲜事）"
    transcript = "\n".join(
        f"{l.get('speaker', '')}：{l.get('text', '')}"
        for l in (g.get("lines") or [])[-14:]) or "（还没人开口）"

    parts = [f"在场的人：{_group_names(g)}，还有 {pname}（玩家）。",
             f"你是 {spk['name']}（{spk['title']}）。",
             f"【此刻】{_ambient_brief()}{_ambient_hint()}",
             f"【魔力】{_magic_text()}",
             (f"【村公告】{_latest_announcement()}"
              if _latest_announcement() else "（今天没有村公告）")]
    if rel_lines:
        parts.append("【你怎么看在场的人】\n" + "\n".join(rel_lines))
    parts.append(f"【最近村里的事】{recent}")
    if g.get("topic"):
        parts.append(f"【这次的话题】{g['topic']}")
    parts.append("【刚才的对话】\n" + transcript)
    parts.append("现在轮到你开口。只输出你说的话本身，不要加名字前缀。")
    return "\n\n".join(parts)


def _clean_group_text(text, name):
    """模型有时会自作主张加「名字：」，去掉。"""
    t = (text or "").strip()
    t = re.sub(r"^\**" + re.escape(name) + r"\**[：:]\s*", "", t)
    t = t.strip().strip("「」\"'“”")
    t = re.sub(r"\s*\n+\s*", " ", t)
    return t[:160].strip()


def _group_pick(g, prefer=None):
    """下一个说话的人：有人被 @ 就让他先说，否则轮流。"""
    members = _group_members(g)
    if not members:
        return None
    if prefer in members:
        g["turn"] = (members.index(prefer) + 1) % len(members)
        return prefer
    i = int(g.get("turn") or 0) % len(members)
    g["turn"] = (i + 1) % len(members)
    return members[i]


def _group_mentioned(g):
    """最后一句里 @ 了谁。"""
    lines = g.get("lines") or []
    if not lines:
        return None
    last = lines[-1].get("text") or ""
    for k in _group_members(g):
        n = NPC_PERSONAS[k]
        if f"@{n['name']}" in last or f"@{k}" in last:
            return k
    return None


def _group_speak(g, spk_key):
    """让一个角色单独生成一句。"""
    npc = NPC_PERSONAS[spk_key]
    client = _client_for(npc)
    msg = client.chat(
        [{"role": "system", "content": _group_system(npc)},
         {"role": "user", "content": _group_user(g, spk_key)}],
        temperature=0.85)
    _record_usage(f"group|{g['id']}", client.last_usage, client.model)
    return _clean_group_text(msg.get("content"), npc["name"])


def _npc_brief(npc):    return (f"身份：{npc.get('role', '')}\n"
            f"性格：{npc.get('personality', '')}\n"
            f"说话风格：{npc.get('speech', '')}")


def _client_for(npc):
    """给某个角色挑客户端：有专属配置就用专属的，否则用全局。"""
    cfg = (npc or {}).get("model") or {}
    if not cfg.get("base_url") and not cfg.get("model"):
        return CLIENT                                  # 没配 → 全局

    key = _load_model_keys().get((npc or {}).get("key"), "")
    return LLMClient(
        api_key=key or os.getenv("OPENAI_API_KEY", ""),
        base_url=cfg.get("base_url") or None,
        model=cfg.get("model") or None,
    )


def _load_usage():
    if os.path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return {}


def _save_usage(d):
    with open(USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _record_usage(key, usage, model):
    """把一次调用的 token 用量累加进去。

    注意：Agent 的 turn_usage 带 calls；client.last_usage 只有 token，
    所以这里要兼容两种形状。
    """
    if not usage:
        return
    calls = int(usage.get("calls") or 0)
    tokens = int(usage.get("total_tokens") or 0)
    if not calls and not tokens:
        return
    if not calls:
        calls = 1                       # 只有 token 的那种，算一次调用

    with LOCK:
        d = _load_usage()
        row = d.get(key) or {"calls": 0, "prompt_tokens": 0,
                             "completion_tokens": 0, "total_tokens": 0}
        for f in ("prompt_tokens", "completion_tokens", "total_tokens"):
            row[f] = int(row.get(f, 0)) + int(usage.get(f, 0))
        row["calls"] = int(row.get("calls", 0)) + calls
        row["model"] = model or row.get("model")
        row["updated"] = datetime.now().isoformat(timespec="seconds")
        d[key] = row
        _save_usage(d)


def _usage_report():
    """整理成按角色分组的报表 + 总计。"""
    d = _load_usage()
    fields = ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
    total = dict.fromkeys(fields, 0)
    items = []
    for key, row in d.items():
        mode, npc_key = parse_key(key)
        npc = NPC_PERSONAS.get(npc_key)
        if npc:
            name = npc["name"]
        elif mode in ("interact", "battle"):
            ka, _, kb = npc_key.partition("+")
            na = (NPC_PERSONAS.get(ka) or {}).get("name", ka)
            nb = (NPC_PERSONAS.get(kb) or {}).get("name", kb)
            name = f"{'对戏' if mode == 'interact' else '对战'} {na}×{nb}"
        elif mode == "assistant":
            name = ASSISTANT_NAME
        else:
            name = key
        item = {"key": key, "name": name, "mode": mode,
                "model": row.get("model"), "updated": row.get("updated")}
        for f in fields:
            item[f] = int(row.get(f, 0))
            total[f] += item[f]
        items.append(item)
    items.sort(key=lambda x: x["total_tokens"], reverse=True)
    return {"items": items, "total": total}


def make_agent(mode, npc_key, state=None, mood_holder=None):
    """按模式建一个智能体；NPC 模式会绑定一份游戏状态。"""
    if mode == "npc":
        npc = get_npc(npc_key) or get_npc("blacksmith")
        if state is None:
            state = get_player()

        def context():
            goods = "、".join(f"{k}({v}金)" for k, v in npc.get("goods", {}).items()) or "无"
            svcs = "、".join(f"{k}({v[0]}金)" for k, v in npc.get("services", {}).items()) or "无"

            # 角色关系
            rel_map = _load_relations().get(npc_key) or {}
            rel_lines = []
            for other_key, desc in rel_map.items():
                other = NPC_PERSONAS.get(other_key)
                if other:
                    rel_lines.append(f"- {other['name']}（{other['title']}）：{desc}")
            rel_txt = "\n".join(rel_lines) or "- （这里没什么熟人）"

            # 村里最近的事
            events = _recent_events(5)
            ev_txt = "\n".join(f"- {e}" for e in events) or "- （最近没什么新鲜事）"

            hint = _ambient_hint()
            ann = _latest_announcement()
            magic = _magic_text()
            spell_list = _npc_spell_list(npc)
            if spell_list:
                left_slot, max_slot = _slots(npc_key)
                zh = {"damage": "伤害", "heal": "治疗", "utility": "场面"}
                spell_txt = "、".join(
                    f"{s['key']}={s['name']}（{s['cost']}位·"
                    f"{zh.get(s['effect'], '场面')}）"
                    for s in spell_list)
                spells_line = (f"【你的法术】{spell_txt}"
                               f"　　法术位 {left_slot}/{max_slot}"
                               "（玩家点名要哪个，就必须调 cast_spell 真放，"
                               "别只用嘴说）\n")
            else:
                spells_line = "【你的法术】（你不会法术，别假装会）\n"
            return ((f"{_ambient_brief()}\n"
                     + (f"{hint}\n" if hint else "")
                     + (f"【魔力】{magic}\n" if magic else "")
                     + (f"【村公告】{ann}\n" if ann else "")
                     + spells_line
                     + f"玩家：{state['player_name']}，"
                     f"生命值 {state['player_hp']}/100，金币 {state['gold']}，"
                     f"背包 {state['inventory'] or '空'}\n"
                     f"本店商品：{goods}\n本店服务：{svcs}\n"
                     f"\n【你认识的人】\n{rel_txt}\n"
                     f"\n【最近村里的事】\n{ev_txt}\n"
                     f"（这些是背景，别生硬罗列，也别每句都报时间天气。"
                     f"但时间天气会影响人怎么做事——天冷、下雨、夜里，"
                     f"你手上的活和说话的样子自然跟着变，不用等玩家提起。）"))

        agent = Agent(_client_for(npc), npc["prompt"],
                      tools=build_npc_tools(
                          state, npc.get("goods"), npc.get("services"),
                          mood_holder,
                          spells=_npc_spell_list(npc),
                          cast_fn=lambda **kw: _cast_spell(
                              npc_key, npc, state, mood_holder, kw)),
                      context_provider=context,
                      memory_turns=npc.get("memory_turns", 30),
                      summarize_batch=npc.get("summarize_batch", 6))
        return agent, state, npc
    agent = Agent(CLIENT, ASSISTANT_SYSTEM,
                  tools=assistant_tools() + extra_tools(),
                  context_provider=lambda: _assistant_context())
    return agent, None, None


def infer_mood(changes):
    """NPC 没主动表态时，根据交易结果推测情绪。"""
    for c in changes:
        if c["kind"] == "player_hp" and c["delta"] > 0:
            return "happy"
    if any(c["kind"] == "item_add" or
           (c["kind"] == "gold" and c["delta"] < 0) for c in changes):
        return "happy"
    return "neutral"


def get_session(session_id, mode, npc_key):
    """取会话：内存里没有就试着读本地存档，再没有才新建。"""
    key = session_key(session_id, mode, npc_key)
    with LOCK:
        if key in SESSIONS:
            return SESSIONS[key]

        saved = load_saved(key)
        holder = {}
        # 玩家状态是全局唯一的，不从存档里恢复
        agent, state, npc = make_agent(mode, npc_key, None, holder)
        if saved:
            agent.load_history(saved.get("messages", []),
                               saved.get("summary", ""),
                               saved.get("summarized_upto"))
            print(f"[读档] {key}（{len(saved.get('messages', []))} 条上下文）")

        SESSIONS[key] = {"agent": agent, "state": state, "npc": npc,
                         "mode": mode, "key": key,
                         "mood_holder": holder,
                         "mood": (saved or {}).get("mood", "neutral")}
        return SESSIONS[key]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                                    # 别刷屏

    def _send(self, code, payload, ctype="application/json", extra=None):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"")

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or "{}") if n else {}

    def _raw_body(self):
        """原样读取请求体（上传 zip / 图片用）。"""
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    # ---------------------------------------------------------- GET
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        maybe_reload_personas()          # 编辑过 characters.json 就自动生效

        if path in ("/", "/index.html"):
            index = os.path.join(WEB_DIR, "index.html")
            if os.path.exists(index):
                with open(index, "rb") as f:
                    # 页面本身不缓存：改完前端刷新就能看到，不用手动清缓存
                    self._send(200, f.read(), "text/html; charset=utf-8",
                               extra={"Cache-Control": "no-store"})
            else:
                self._send(404, {"error": "web/index.html 不存在"})
            return

        if path == "/health":
            self._send(200, {"ok": True, "model": CLIENT.model,
                             "key_ready": CLIENT.ready()})
            return

        if path == "/call/info":
            self._send(200, {
                "ok": True,
                "app": os.path.isdir(VOICE_APP),
                "whisper": ("ready" if _WHISPER["model"]
                            else "missing" if _WHISPER["error"] else "idle"),
                "model": os.getenv("WHISPER_MODEL", "small"),
                "error": _WHISPER["error"],
            })
            return

        if path == "/announcements":
            out = []
            if os.path.exists(WORLD_FILE):
                try:
                    with open(WORLD_FILE, encoding="utf-8") as f:
                        d = json.load(f)
                    out = [{"t": e.get("t", ""), "text": e.get("text", "")}
                           for e in (d if isinstance(d, list) else [])
                           if isinstance(e, dict)
                           and e.get("kind") == "announcement"][-10:]
                except Exception:
                    out = []
            self._send(200, out)
            return

        if path == "/slots":
            q = parse_qs(parsed.query)
            key = q.get("npc", [""])[0]
            npc = NPC_PERSONAS.get(key)
            if not npc:
                self._send(404, {"error": "没有这个角色"})
                return
            left, mx = _slots(key)
            self._send(200, {"ok": True, "left": left, "max": mx,
                             "spells": _npc_spell_list(npc)})
            return

        if path == "/appearance":
            self._send(200, _appearance())
            return

        if path == "/ambient":
            # 时间 + 真实天气（10 分钟缓存；?refresh=1 强制刷新）
            force = parse_qs(parsed.query).get("refresh", ["0"])[0] not in ("0", "")
            self._send(200, _ambient_payload(force=force))
            return

        if path == "/voices":
            q = parse_qs(parsed.query)
            only_zh = q.get("all", ["0"])[0] != "1"
            self._send(200, _say_voices(only_zh))
            return

        if path == "/npcs":
            q = parse_qs(parsed.query)
            show_all = q.get("all", ["0"])[0] == "1"
            disabled = disabled_set()
            keys_map = _load_model_keys()
            hp_map = _load_npc_state()
            out = []
            for k, v in NPC_PERSONAS.items():
                enabled = k not in disabled
                if not enabled and not show_all:
                    continue                    # 停用的角色默认不返回
                mx = _npc_stats(v)["hp"]
                cur = (hp_map.get(k) or {}).get("hp")
                out.append({"key": k, "name": v["name"], "title": v["title"],
                            "avatar": v.get("avatar", ""), "enabled": enabled,
                            "voice": v.get("voice"),
                            "model": v.get("model"),
                            "has_key": bool(keys_map.get(k)),
                            "stats": v.get("stats"),
                            "hp": mx if cur is None else max(0, min(mx, int(cur))),
                            "max_hp": mx})
            self._send(200, out)
            return

        if path == "/assistant":
            self._send(200, {"name": ASSISTANT_NAME,
                             "avatar": ASSISTANT_AVATAR})
            return

        if path == "/history":
            q = parse_qs(parsed.query)
            session_id = q.get("session", [""])[0]
            mode = q.get("mode", ["assistant"])[0]
            npc_key = q.get("npc", ["blacksmith"])[0]
            key = session_key(session_id, mode, npc_key)
            sess = SESSIONS.get(key)
            if sess is not None:
                msgs, state, mood = (sess["agent"].history, sess["state"],
                                     sess.get("mood", "neutral"))
                summary = sess["agent"].summary
            else:
                saved = load_saved(key) or {}
                msgs, state = saved.get("messages", []), saved.get("state")
                mood = saved.get("mood", "neutral")
                summary = saved.get("summary", "")
            convo = [{"role": m.get("role"), "content": m.get("content")}
                     for m in msgs
                     if m.get("role") in ("user", "assistant") and m.get("content")]
            self._send(200, {"messages": convo, "state": state, "mood": mood,
                             "summary": summary})
            return

        if path == "/state":
            q = parse_qs(parsed.query)
            session_id = q.get("session", [""])[0]
            mode = q.get("mode", ["npc"])[0]
            npc_key = q.get("npc", ["blacksmith"])[0]
            sess = SESSIONS.get(session_key(session_id, mode, npc_key))
            self._send(200, {"state": sess["state"] if sess else None})
            return

        if path == "/export_character":
            q = parse_qs(parsed.query)
            key = q.get("key", [""])[0]
            if key not in NPC_PERSONAS:
                self._send(404, {"error": f"没有叫「{key}」的角色"})
                return
            try:
                data = build_character_zip(key)
            except Exception as e:
                self._send(500, {"error": str(e)})
                return
            name = NPC_PERSONAS[key].get("name", key)
            self._send(200, data, "application/zip",
                       extra={"Content-Disposition":
                              f"attachment; filename=character-{key}.zip"})
            return

        if path == "/backup":
            # 整包备份（不含 .env / API key），浏览器直接下载
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._send(200, build_zip(with_key=False), "application/zip",
                       extra={"Content-Disposition":
                              f'attachment; filename="agent-backup-{stamp}.zip"'})
            return

        if path == "/memory":
            q = parse_qs(parsed.query)
            mode = q.get("mode", ["npc"])[0]
            npc_key = q.get("npc", ["blacksmith"])[0]
            sess = get_session("", mode, npc_key)
            agent = sess["agent"]
            name = (sess.get("npc") or {}).get("name") or ASSISTANT_NAME
            self._send(200, {
                "mode": mode, "npc": npc_key, "name": name,
                "summary": agent.summary,
                "messages_total": len(agent.messages),
                "turns": sum(1 for m in agent.messages
                             if m.get("role") == "user"),
                "window": agent.memory_turns,
                "batch": agent.summarize_batch,
            })
            return

        if path == "/undo_info":
            d = load_erased()
            if d:
                self._send(200, {"available": True, "key": d.get("key"),
                                 "name": d.get("name"), "title": d.get("title"),
                                 "time": d.get("time")})
            else:
                self._send(200, {"available": False})
            return

        if path == "/player":
            self._send(200, get_player())
            return

        if path == "/battles":
            recs = []
            if os.path.exists(BATTLES_FILE):
                try:
                    with open(BATTLES_FILE, encoding="utf-8") as f:
                        recs = json.load(f)
                except Exception:
                    recs = []
            self._send(200, recs[-20:])
            return

        if path == "/group/list":
            rooms = sorted(_load_groups().values(),
                           key=lambda g: g.get("created", ""), reverse=True)
            self._send(200, [_group_summary(g) for g in rooms[:20]])
            return

        if path == "/group":
            g = _load_groups().get(parse_qs(parsed.query).get("id", [""])[0])
            if not g:
                self._send(404, {"error": "没有这个群"})
                return
            self._send(200, _group_public(g))
            return

        if path == "/usage":
            self._send(200, _usage_report())
            return

        if path == "/saves":
            self._send(200, list_saves())
            return

        if path == "/export":
            q = parse_qs(parsed.query)
            session_id = q.get("session", [""])[0]
            self._send(200, export_bundle(session_id or None))
            return

        # 静态资源：web/ 下的任意文件（头像、预览页等）
        rel = path.lstrip("/")
        file_path = os.path.abspath(os.path.join(WEB_DIR, rel))
        if rel and file_path.startswith(os.path.abspath(WEB_DIR)) \
                and os.path.isfile(file_path):
            ctype = MIME.get(os.path.splitext(file_path)[1].lower(),
                             "application/octet-stream")
            with open(file_path, "rb") as f:
                self._send(200, f.read(), ctype)
            return

        self._send(404, {"error": "not found"})

    # ---------------------------------------------------------- POST
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        maybe_reload_personas()          # 编辑过 characters.json 就自动生效

        # ⚠️ 这几个接口要原样读请求体（上传 zip），必须放在 _body() 之前，
        # 否则 json 解析会先把二进制弄坏
        if path in ("/restore_backup", "/inspect_pack", "/import_character",
                    "/upload_avatar", "/upload_bg"):
            data = self._raw_body()
            if not data:
                self._send(400, {"error": "没有收到文件内容"})
                return

            # 先探一眼这是「整包备份」还是「单角色包」
            if path == "/inspect_pack":
                try:
                    self._send(200, inspect_pack(data))
                except Exception as e:
                    self._send(400, {"error": f"不是有效的包：{e}"})
                return

            # 上传页面背景
            if path == "/upload_bg":
                if len(data) > 30 * 1024 * 1024:
                    self._send(400, {"error": "文件太大了（动图上限 30MB）"})
                    return
                try:
                    rel = _store_bg(data)
                except ValueError as e:
                    self._send(400, {"error": str(e)})
                    return
                except Exception as e:
                    self._send(500, {"error": f"保存失败：{e}"})
                    return
                self._send(200, {"ok": True, "path": rel,
                                 "bg_list": _list_bg()})
                return

            # 导入单角色包
            if path == "/import_character":
                q = parse_qs(parsed.query)
                mode = q.get("mode", ["copy"])[0]
                if mode not in ("copy", "overwrite"):
                    mode = "copy"
                try:
                    r = import_character(data, mode=mode)
                except Exception as e:
                    self._send(400, {"error": f"导入失败：{e}"})
                    return
                with LOCK:
                    SESSIONS.clear()
                reload_personas()
                self._send(200, {"ok": True, **r})
                return

            # 上传头像
            if path == "/upload_avatar":
                q = parse_qs(parsed.query)
                key = q.get("key", [""])[0]
                slot = q.get("slot", ["neutral"])[0]
                if key not in NPC_PERSONAS:
                    self._send(400, {"error": f"没有叫「{key}」的角色"})
                    return
                if slot not in AVATAR_SLOTS:
                    self._send(400, {"error": "slot 只能是 "
                                              "neutral / happy / angry"})
                    return
                is_gif = data[:6] in (b"GIF87a", b"GIF89a")
                cap = 15 * 1024 * 1024 if is_gif else 8 * 1024 * 1024
                if len(data) > cap:
                    self._send(400, {"error": f"图片太大了（上限 "
                                              f"{'15' if is_gif else '8'}MB）"})
                    return
                try:
                    rel, ext = _store_avatar(key, slot, data)
                except ValueError as e:
                    self._send(400, {"error": str(e)})
                    return
                changed = _set_character_avatar(key, rel) \
                    if slot == "neutral" else False
                reload_personas()
                self._send(200, {"ok": True, "slot": slot, "path": rel,
                                 "ext": ext, "avatar_updated": changed,
                                 "name": NPC_PERSONAS[key]["name"]})
                return

            # 整包恢复
            try:
                actions, _ = restore_backup(data, yes=True)
            except Exception as e:
                self._send(400, {"error": f"不是有效的备份包：{e}"})
                return
            if not actions:
                self._send(400, {"error": "备份包是空的"})
                return
            # 内存缓存全部丢掉，让它从刚恢复的文件重新读
            with LOCK:
                SESSIONS.clear()
            reload_personas()
            over = sum(1 for _, exists in actions if exists)
            self._send(200, {
                "ok": True, "files": len(actions),
                "added": len(actions) - over, "overwritten": over,
                "env_restored": any(rel == ".env" for rel, _ in actions),
            })
            return

        try:
            body = self._body()
        except Exception as e:
            self._send(400, {"error": f"请求体解析失败：{e}"})
            return

        if path == "/battle":
            # 对战：real=True 时先算数值，再让模型照着战报写；否则纯叙事
            a_key = body.get("a") or ""
            b_key = body.get("b") or ""
            topic = (body.get("topic") or "").strip()
            record = body.get("record", True) is not False
            real = bool(body.get("real"))
            a = NPC_PERSONAS.get(a_key)
            b = NPC_PERSONAS.get(b_key)
            if not a or not b:
                self._send(400, {"error": "a / b 都要是有效的角色 key"})
                return
            if a_key == b_key:
                self._send(400, {"error": "两个角色不能是同一个"})
                return

            rel = _load_relations()
            rel_ab = (rel.get(a_key) or {}).get(b_key, "")
            rel_ba = (rel.get(b_key) or {}).get(a_key, "")

            duel = None
            if real:
                duel = _run_duel(a_key, b_key)
                transcript = _duel_transcript(duel, a, b, a_key, b_key)
                wname = (NPC_PERSONAS[duel["winner"]]["name"]
                         if duel["winner"] else "平手")
                prompt = (
                    "下面这场战斗的过程和胜负**已经算好了**，你只负责把它写成"
                    "好看的打斗场面，不许改结果。\n\n"
                    f"【角色 A】{a['name']}（{a['title']}）\n{_npc_brief(a)}\n\n"
                    f"【角色 B】{b['name']}（{b['title']}）\n{_npc_brief(b)}\n\n"
                    "【他们怎么看对方】\n"
                    f"{a['name']} 看 {b['name']}：{rel_ab or '不太熟'}\n"
                    f"{b['name']} 看 {a['name']}：{rel_ba or '不太熟'}\n\n"
                    f"【此刻】{_ambient_brief()}{_ambient_hint()}\n\n"
                    + (f"【为什么打起来】{topic}\n\n" if topic else "")
                    + f"【已经打完了，过程如下】\n{transcript}\n\n"
                    + f"【最终胜负】{wname}\n\n"
                    + "要求：\n"
                    "- 写成 4~6 句，格式 `名字：台词或动作`，动作放括号里。\n"
                    "- 需要环境描写用 `旁白：...`。\n"
                    "- **绝对不能改胜负**；也不要编造和上面不一致的伤害数字，"
                    "可以只说「一记重击」这种模糊描述。\n"
                    "- 别写血腥细节，点到为止。\n"
                    "- 最后一行：`结果：谁赢/输/平手，一句话`。\n"
                )
            else:
                prompt = (
                    "你要写一场两个角色之间的短打斗，玩家在旁边看着。\n\n"
                    f"【角色 A】{a['name']}（{a['title']}）\n{_npc_brief(a)}\n\n"
                    f"【角色 B】{b['name']}（{b['title']}）\n{_npc_brief(b)}\n\n"
                    "【他们怎么看对方】\n"
                    f"{a['name']} 看 {b['name']}：{rel_ab or '不太熟'}\n"
                    f"{b['name']} 看 {a['name']}：{rel_ba or '不太熟'}\n\n"
                    f"【此刻】{_ambient_brief()}{_ambient_hint()}\n\n"
                    + (f"【为什么打起来】{topic}\n\n" if topic else "")
                    + "要求：\n"
                    "- 3~5 个回合，每行一句，节奏要快。\n"
                    "- 格式：`名字：台词或动作`，动作细节放括号里，例如 `老铁：（锤子横扫）躲得挺快`。\n"
                "- 名字只用上面两个；需要环境描写时用 `旁白：...`。\n"
                "- 如果某个角色明显不适合动武，就让他用自己的方式应战"
                "（耍嘴皮子、闪避、搬规矩、耍赖都行），别硬写。\n"
                "- 别写血腥残忍的细节，点到为止。\n"
                "- 最后单独一行：`结果：谁赢/输/平手，一句话说明原因`。\n"
            )

            try:
                client = _client_for(b)
                msg = client.chat([{"role": "user", "content": prompt}],
                                  temperature=0.85)
                raw = msg.get("content") or ""
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
                return

            lines, result = [], ""
            for ln in raw.splitlines():
                ln = ln.strip().strip("*#- ")
                if not ln:
                    continue
                if ln.startswith("结果：") or ln.startswith("结果:"):
                    result = ln[3:].lstrip("：: ").strip()
                    continue
                m = re.match(r"^([^：:]{1,12})[：:]\s*(.+)$", ln)
                if m and m.group(1).strip() in (a["name"], b["name"], "旁白"):
                    lines.append({"speaker": m.group(1).strip(),
                                  "text": m.group(2).strip().strip("「」\"'")})
            if not lines:
                lines = [{"speaker": "旁白", "text": raw[:240]}]

            # 真打模式：用引擎算出的结果覆盖模型写的「结果」，保证数字不会漂
            if real and duel:
                wname = (NPC_PERSONAS[duel["winner"]]["name"]
                         if duel["winner"] else "平手")
                ah, bh = duel["hp"][a_key], duel["hp"][b_key]
                amx, bmx = duel["maxhp"][a_key], duel["maxhp"][b_key]
                result = (f"{wname} 胜"
                          if duel["winner"] else "平手")
                result += f"（{a['name']} {ah}/{amx}，{b['name']} {bh}/{bmx}）"
                _log_battle({
                    "id": f"b_{int(datetime.now().timestamp())}",
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "a": a_key, "b": b_key, "topic": topic,
                    "winner": duel["winner"],
                    "rounds": duel["rounds"],
                    "hp": duel["hp"], "maxhp": duel["maxhp"],
                })

            if record and result:
                _append_event(
                    f"{a['name']}和{b['name']}交过手，{result[:40]}")

            _record_usage(f"battle|{a_key}+{b_key}", client.last_usage,
                          client.model)
            payload = {"ok": True, "lines": lines, "result": result}
            if real and duel:
                payload["hp"] = duel["hp"]
                payload["maxhp"] = duel["maxhp"]
                payload["winner"] = duel["winner"]
            self._send(200, payload)
            return

        if path == "/interact":
            # 让两个角色演一段对手戏
            a_key = body.get("a") or ""
            b_key = body.get("b") or ""
            topic = (body.get("topic") or "").strip()
            a = NPC_PERSONAS.get(a_key)
            b = NPC_PERSONAS.get(b_key)
            if not a or not b:
                self._send(400, {"error": "a / b 都要是有效的角色 key"})
                return
            if a_key == b_key:
                self._send(400, {"error": "两个角色不能是同一个"})
                return

            rel = _load_relations()
            rel_ab = (rel.get(a_key) or {}).get(b_key, "")
            rel_ba = (rel.get(b_key) or {}).get(a_key, "")
            recent = "；".join(_recent_events(5)) or "（没什么新鲜事）"

            prompt = (
                "你要写一段两个角色之间的自然对话，玩家在旁边看着。\n\n"
                f"【角色 A】{a['name']}（{a['title']}）\n{_npc_brief(a)}\n\n"
                f"【角色 B】{b['name']}（{b['title']}）\n{_npc_brief(b)}\n\n"
                "【他们怎么看对方】\n"
                f"{a['name']} 看 {b['name']}：{rel_ab or '不太熟'}\n"
                f"{b['name']} 看 {a['name']}：{rel_ba or '不太熟'}\n\n"
                f"【此刻】{_ambient_brief()}{_ambient_hint()}\n"
                f"【最近村里的事】{recent}\n\n"
                + (f"【这次要聊的话题】{topic}\n\n" if topic else "")
                + "要求：\n"
                "- 写 4~6 句对话，每句单独一行。\n"
                "- 格式严格为「名字：台词」，名字只用上面给的两个。\n"
                "- 不要旁白、不要解释、不要编号、不要给台词加引号。\n"
            )

            try:
                client = _client_for(b)
                msg = client.chat([{"role": "user", "content": prompt}],
                                  temperature=0.8)
                raw = msg.get("content") or ""
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
                return

            lines = []
            for ln in raw.splitlines():
                ln = ln.strip().strip("*#- ")
                if not ln:
                    continue
                m = re.match(r"^([^：:]{1,12})[：:]\s*(.+)$", ln)
                if m and m.group(1).strip() in (a["name"], b["name"]):
                    lines.append({"speaker": m.group(1).strip(),
                                  "text": m.group(2).strip().strip("「」\"'")})
            if not lines:
                lines = [{"speaker": a["name"], "text": raw[:200]}]

            _record_usage(f"interact|{a_key}+{b_key}", client.last_usage,
                          client.model)
            self._send(200, {"ok": True, "lines": lines})
            return

        if path == "/tts":
            # 把一段文字念出来，返回 WAV 音频
            voice, rate, pitch = _resolve_voice(body)
            try:
                data = _tts_wav(body.get("text", ""), voice=voice,
                                rate=rate, pitch=pitch)
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            except FileNotFoundError:
                self._send(501, {"error": "这台机器没有 say 命令（不是 macOS）"})
                return
            except Exception as e:
                self._send(500, {"error": f"语音生成失败：{e}"})
                return
            self._send(200, data, "audio/wav")
            return

        if path == "/call/listen":
            try:
                r = voice_listen(max_sec=body.get("max") or 12,
                                 silence=body.get("silence") or 1.0)
            except Exception as e:
                self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                return
            self._send(200, dict(ok=True, **r))
            return

        if path == "/appearance":
            try:
                self._send(200, {"ok": True, **_save_appearance(body)})
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return

        if path == "/bg/delete":
            name = os.path.basename((body.get("name") or "").strip())
            p = os.path.join(BG_DIR, name) if name else ""
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
            a = _appearance()
            if name and a["bg"].endswith(name):
                a = _save_appearance({"theme": a["theme"], "bg": "",
                                      "dim": a["dim"], "blur": a["blur"]})
            self._send(200, {"ok": True, **a})
            return

        if path == "/announce":
            text = (body.get("text") or "").strip()
            if not text:
                self._send(400, {"error": "公告不能是空的"})
                return
            if len(text) > 200:
                self._send(400, {"error": "公告太长了（上限 200 字）"})
                return
            _append_event(text, kind="announcement")
            self._send(200, {"ok": True, "text": text})
            return

        if path == "/call/stop_speak":
            with _PLAYING["lock"]:
                proc = _PLAYING["proc"]
                _PLAYING["stopped"] = proc is not None
            killed = False
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()          # afplay 收到 SIGTERM 就停
                    killed = True
                except Exception:
                    pass
            self._send(200, {"ok": True, "stopped": killed})
            return

        if path == "/call/stop":
            # 挂断 / 关页面时调用：立刻停掉正在录音的进程
            stop_voice_app()
            self._send(200, {"ok": True})
            return

        if path == "/call/warm":
            # 后台把模型加载好，前端轮询 /call/info
            if _WHISPER["model"] is None and not _WHISPER["error"]:
                threading.Thread(target=_whisper_model, daemon=True).start()
            self._send(200, {"ok": True})
            return

        if path == "/speak":
            # 生成后直接在服务器这台机器的扬声器上播放（内嵌浏览器常常没声音）
            voice, rate, pitch = _resolve_voice(body)
            try:
                data = _tts_wav(body.get("text", ""), voice=voice,
                                rate=rate, pitch=pitch)
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            except FileNotFoundError:
                self._send(501, {"error": "这台机器没有 say / afplay（不是 macOS）"})
                return
            except Exception as e:
                self._send(500, {"error": f"语音生成失败：{e}"})
                return
            interrupted = False
            if body.get("wait"):
                _play_bytes(data, register=True)   # 通话模式：播完才返回
                with _PLAYING["lock"]:
                    interrupted = _PLAYING["stopped"]
            else:
                threading.Thread(target=_play_bytes, args=(data,),
                                 daemon=True).start()
            self._send(200, {"ok": True, "bytes": len(data),
                             "voice": voice, "rate": rate, "pitch": pitch,
                             "interrupted": interrupted})
            return
        if path == "/set_voice":
            # 给某个角色设定专属音色
            key = body.get("key", "")
            if key not in NPC_PERSONAS:
                self._send(400, {"error": f"没有叫「{key}」的角色"})
                return
            cfg = body.get("voice")
            if cfg is not None and not isinstance(cfg, dict):
                self._send(400, {"error": "voice 要传对象，或者传 null 清空"})
                return
            for e in personas_mod.CHARACTERS:
                if e.get("key") == key:
                    if cfg:
                        clean = {}
                        for k in ("name", "rate", "pitch"):
                            if cfg.get(k) not in (None, ""):
                                clean[k] = cfg[k]
                        e["voice"] = clean if clean else None
                    else:
                        e["voice"] = None
                    personas_mod.write_raw(personas_mod.CHARACTERS)
                    break
            reload_personas()
            self._send(200, {"ok": True, "key": key,
                             "voice": (NPC_PERSONAS.get(key) or {}).get("voice")})
            return

        if path == "/chat":
            session_id = body.get("session") or "default"
            mode = body.get("mode", "assistant")
            npc_key = body.get("npc", "blacksmith")
            message = body.get("message", "").strip()
            if not message:
                self._send(400, {"error": "message 不能为空"})
                return

            if not CLIENT.ready():
                self._send(500, {"error": "服务端没有配置 API key（见 .env）"})
                return
            try:
                sess = get_session(session_id, mode, npc_key)
                before = copy.deepcopy(sess["state"])
                sess["mood_holder"].pop("mood", None)     # 清掉上一轮的表情
                sess["mood_holder"]["casts"] = 0          # 每轮重置施法次数
                reply = sess["agent"].chat(message)
                changes = diff_state(before, sess["state"]) if \
                    sess["state"] is not None else []
                # 优先用 NPC 自己表态的表情，没有就按交易结果推测
                mood = sess["mood_holder"].get("mood")
                if not mood and mode == "npc":
                    mood = infer_mood(changes)
                sess["mood"] = mood or sess.get("mood", "neutral")
                save_session(sess["key"], sess)          # 每轮都落盘
                if sess["state"] is not None:
                    save_player(sess["state"])           # 玩家档案也落盘
                _record_usage(sess["key"], sess["agent"].turn_usage,
                              getattr(sess["agent"].client, "model", None))
                # 有交易就记一笔"村里的事"，别的角色也能听说
                ev = _event_from(sess.get("npc") or {}, changes)
                if ev:
                    _append_event(ev)
                self._send(200, {"reply": reply, "state": sess["state"],
                                 "changes": changes, "mood": sess["mood"],
                                 "summary": sess["agent"].summary})
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return

        if path == "/reset":
            # 只清「玩家 ↔ 这一个角色」的记忆，不影响其他角色
            mode = body.get("mode", "assistant")
            npc_key = body.get("npc", "-")
            key = session_key("", mode, npc_key)
            with LOCK:
                SESSIONS.pop(key, None)
                try:
                    os.remove(_save_path(key))
                except OSError:
                    pass
            self._send(200, {"ok": True, "key": key})
            return

        if path == "/toggle_npc":
            key = body.get("key", "")
            enabled = bool(body.get("enabled", True))
            if key not in NPC_PERSONAS:
                self._send(400, {"error": f"没有叫「{key}」的角色"})
                return
            s = load_settings()
            d = set(s.get("disabled", []))
            if enabled:
                d.discard(key)
            else:
                d.add(key)
            s["disabled"] = sorted(d)
            save_settings(s)
            self._send(200, {"ok": True, "key": key, "enabled": enabled})
            return

        if path == "/memory":
            mode = body.get("mode", "npc")
            npc_key = body.get("npc", "blacksmith")
            sess = get_session("", mode, npc_key)
            sess["agent"].summary = (body.get("summary") or "").strip()
            save_session(sess["key"], sess)
            self._send(200, {"ok": True, "summary": sess["agent"].summary})
            return

        if path == "/memory/compress":
            # 手动整理：不用等窗口溢出，现在就把当前对话压成长期记忆
            mode = body.get("mode", "npc")
            npc_key = body.get("npc", "blacksmith")
            sess = get_session("", mode, npc_key)
            agent = sess["agent"]
            try:
                keep = int(body.get("keep_turns", 3))
            except (TypeError, ValueError):
                keep = 3
            did = agent.compress_now(keep_turns=max(0, min(keep, 20)))
            save_session(sess["key"], sess)
            self._send(200, {"ok": True, "compressed": did,
                             "summary": agent.summary,
                             "messages_total": len(agent.messages)})
            return

        if path == "/memory/clear":
            # 只清长期记忆（摘要），保留最近窗口内的对话
            mode = body.get("mode", "npc")
            npc_key = body.get("npc", "blacksmith")
            sess = get_session("", mode, npc_key)
            agent = sess["agent"]
            agent.summary = ""
            agent.summarized_upto = agent._window_start()   # 窗口外的当作已忘掉
            save_session(sess["key"], sess)
            self._send(200, {"ok": True})
            return

        if path == "/player":
            # 编辑玩家档案（血量 / 金币 / 背包 / 名字）
            p = get_player()
            if "player_name" in body:
                p["player_name"] = str(body["player_name"])[:20] or "旅人"
            if "player_hp" in body:
                try:
                    p["player_hp"] = max(0, min(100, int(body["player_hp"])))
                except (TypeError, ValueError):
                    pass
            if "gold" in body:
                try:
                    p["gold"] = max(0, int(body["gold"]))
                except (TypeError, ValueError):
                    pass
            if "inventory" in body and isinstance(body["inventory"], list):
                p["inventory"] = [str(x)[:40] for x in body["inventory"]][:100]
            save_player(p)
            self._send(200, {"ok": True, "player": p})
            return

        if path == "/group/create":
            members = [k for k in (body.get("members") or [])
                       if k in NPC_PERSONAS]
            members = list(dict.fromkeys(members))[:6]
            if len(members) < 2:
                self._send(400, {"error": "至少要选两个角色"})
                return
            gid = "g_" + datetime.now().strftime("%m%d%H%M") + str(random.randint(10, 99))
            g = {"id": gid,
                 "name": (body.get("name") or "村口闲聊").strip()[:20],
                 "topic": (body.get("topic") or "").strip()[:100],
                 "members": members, "turn": 0, "lines": [],
                 "created": datetime.now().isoformat(timespec="seconds")}
            with LOCK:
                d = _load_groups()
                d[gid] = g
                _save_groups(d)
            self._send(200, {"ok": True, "room": _group_public(g)})
            return

        if path == "/group/chat":
            gid = body.get("id") or ""
            g = _load_groups().get(gid)
            if not g:
                self._send(404, {"error": "没有这个群"})
                return
            text = (body.get("message") or "").strip()
            rounds = max(1, min(8, int(body.get("rounds") or 1)))

            out = []
            if text:
                line = {"speaker": get_player().get("player_name") or "旅人",
                        "text": text[:200], "role": "player"}
                g.setdefault("lines", []).append(line)
                out.append(line)

            for _ in range(rounds):
                k = _group_pick(g, _group_mentioned(g))
                if not k:
                    break
                try:
                    said = _group_speak(g, k)
                except Exception as e:
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})
                    return
                if not said:
                    continue
                line = {"speaker": NPC_PERSONAS[k]["name"], "text": said,
                        "key": k}
                g["lines"].append(line)
                out.append(line)

            g["lines"] = g["lines"][-200:]
            with LOCK:
                d = _load_groups()
                d[gid] = g
                _save_groups(d)
            self._send(200, {"ok": True, "lines": out,
                             "room": _group_public(g)})
            return

        if path == "/group/delete":
            gid = body.get("id") or ""
            with LOCK:
                d = _load_groups()
                existed = d.pop(gid, None) is not None
                _save_groups(d)
            self._send(200, {"ok": True, "deleted": existed})
            return

        if path == "/npc/heal":
            # 全员回满（或指定一个角色）
            key = body.get("key")
            with LOCK:
                d = _load_npc_state()
                if key:
                    d.pop(key, None)
                else:
                    d.clear()
                _save_npc_state(d)
            reload_personas()
            self._send(200, {"ok": True})
            return

        if path == "/player/reset":
            p = get_player()
            p.clear()
            p.update(DEFAULT_PLAYER)
            save_player(p)
            self._send(200, {"ok": True, "player": p})
            return

        if path == "/usage/reset":
            _save_usage({})
            self._send(200, {"ok": True})
            return

        if path == "/set_model":
            # 给某个角色设专属模型（key 单独存，不进 characters.json）
            key = body.get("key", "")
            if key not in NPC_PERSONAS:
                self._send(400, {"error": f"没有叫「{key}」的角色"})
                return
            cfg = body.get("model")
            if cfg is not None and not isinstance(cfg, dict):
                self._send(400, {"error": "model 要传对象，或传 null 清除"})
                return

            clean = None
            if cfg:
                clean = {}
                for f in ("base_url", "model"):
                    if cfg.get(f):
                        clean[f] = str(cfg[f]).strip()
                if not clean:
                    clean = None

            for e in personas_mod.CHARACTERS:
                if e.get("key") == key:
                    e["model"] = clean
                    personas_mod.write_raw(personas_mod.CHARACTERS)
                    break

            # API key：没传=不动，传空串=清掉，传值=更新
            api_key = (cfg or {}).get("api_key", None)
            keys_map = _load_model_keys()
            if api_key is not None:
                if api_key.strip():
                    keys_map[key] = api_key.strip()
                else:
                    keys_map.pop(key, None)
                _save_model_keys(keys_map)

            reload_personas()
            # 丢掉这个角色的会话，让新模型立刻生效
            with LOCK:
                for k in [k for k in list(SESSIONS) if parse_key(k)[1] == key]:
                    SESSIONS.pop(k, None)

            self._send(200, {"ok": True, "key": key,
                             "model": (NPC_PERSONAS.get(key) or {}).get("model"),
                             "has_key": bool(keys_map.get(key))})
            return

        if path == "/test_model":
            # 试连一下这个模型配置
            base_url = (body.get("base_url") or "").strip()
            model = (body.get("model") or "").strip()
            api_key = (body.get("api_key") or "").strip()
            key = body.get("key") or ""

            if not api_key and key:
                api_key = _load_model_keys().get(key, "")
            if not api_key:
                api_key = CLIENT.api_key

            try:
                c = LLMClient(api_key=api_key,
                              base_url=base_url or None,
                              model=model or None)
                msg = c.chat([{"role": "user", "content": "只回复两个字：通了"}],
                             temperature=0)
                self._send(200, {"ok": True,
                                 "reply": (msg.get("content") or "")[:60],
                                 "model": c.model, "base_url": c.base_url})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)[:300]})
            return

        if path == "/reorder_characters":
            # 按传进来的 key 顺序重排角色表
            keys = body.get("keys")
            if not isinstance(keys, list) or not keys:
                self._send(400, {"error": "keys 要是数组"})
                return
            by_key = {e.get("key"): e for e in personas_mod.CHARACTERS}
            if set(keys) != set(by_key):
                self._send(400, {"error": "keys 和现有角色对不上，刷新页面试试"})
                return
            personas_mod.CHARACTERS[:] = [by_key[k] for k in keys]
            personas_mod.write_raw(personas_mod.CHARACTERS)
            reload_personas()
            self._send(200, {"ok": True, "order": keys})
            return

        if path == "/erase_npc":
            key = body.get("key", "")
            if key not in NPC_PERSONAS:
                self._send(400, {"error": f"没有叫「{key}」的角色"})
                return
            npc = NPC_PERSONAS[key]

            # 1) 先把要删的东西收进回收站（只留最近一次，旧的直接丢掉）
            trash = {"key": key, "name": npc.get("name", key),
                     "title": npc.get("title", ""),
                     "time": datetime.now().isoformat(timespec="seconds"),
                     "character": None, "index": -1,
                     "avatars": {}, "save": None}
            saved = load_saved(session_key("", "npc", key))
            if saved:
                trash["save"] = saved
            avatar_files = []
            if os.path.isdir(AVATAR_DIR):
                for fn in os.listdir(AVATAR_DIR):
                    stem = os.path.splitext(fn)[0]
                    if stem == key or stem.startswith(key + "-"):
                        avatar_files.append(fn)
                        try:
                            with open(os.path.join(AVATAR_DIR, fn), "rb") as f:
                                trash["avatars"][fn] = base64.b64encode(
                                    f.read()).decode()
                        except OSError:
                            pass

            # 2) 从角色表里移除 —— 纯数据操作，不碰任何 Python 代码
            entry, index = personas_mod.remove_character(key)
            trash["character"] = entry
            trash["index"] = index
            reload_personas()

            # 3) 删会话与存档
            with LOCK:
                for k in [k for k in list(SESSIONS) if parse_key(k)[1] == key]:
                    SESSIONS.pop(k, None)
            try:
                os.remove(_save_path(session_key("", "npc", key)))
            except OSError:
                pass

            # 4) 删头像文件
            removed = []
            for fn in avatar_files:
                try:
                    os.remove(os.path.join(AVATAR_DIR, fn))
                    removed.append(fn)
                except OSError:
                    pass

            # 5) 兜底记录 + 写回收站（回收站里存的是角色数据，不是代码）
            s = load_settings()
            s["deleted"] = sorted(set(s.get("deleted", [])) | {key})
            s["disabled"] = sorted(set(s.get("disabled", [])) - {key})
            save_settings(s)
            save_erased(trash)

            self._send(200, {"ok": True, "key": key,
                             "avatars_removed": removed,
                             "removed_from_table": entry is not None})
            return

        if path == "/undo_erase":
            trash = load_erased()
            if not trash:
                self._send(400, {"error": "没有可撤销的抹除"})
                return
            key = trash.get("key", "")

            # 1) 头像写回
            restored = 0
            os.makedirs(AVATAR_DIR, exist_ok=True)
            for fn, b64 in (trash.get("avatars") or {}).items():
                try:
                    with open(os.path.join(AVATAR_DIR, fn), "wb") as f:
                        f.write(base64.b64decode(b64))
                    restored += 1
                except Exception:
                    pass

            # 2) 角色条目插回角色表
            entry = trash.get("character")
            table_ok = False
            if entry:
                personas_mod.insert_character(entry, trash.get("index", -1))
                reload_personas()
                table_ok = True

            # 3) 存档写回
            if trash.get("save"):
                try:
                    with open(_save_path(session_key("", "npc", key)), "w",
                              encoding="utf-8") as f:
                        json.dump(trash["save"], f, ensure_ascii=False, indent=1)
                except OSError:
                    pass

            # 4) 从 deleted 里放出来
            s = load_settings()
            s["deleted"] = sorted(set(s.get("deleted", [])) - {key})
            save_settings(s)

            clear_erased()          # 撤完就清空，不留更早的

            self._send(200, {"ok": True, "key": key, "name": trash.get("name"),
                             "avatars_restored": restored,
                             "table_restored": table_ok})
            return

        if path == "/delete_save":
            key = body.get("key", "")
            if not key:
                self._send(400, {"error": "缺少 key"})
                return
            with LOCK:
                SESSIONS.pop(key, None)
                try:
                    os.remove(_save_path(key))
                except OSError:
                    pass
            self._send(200, {"ok": True})
            return

        if path == "/import":
            saves = body.get("saves") if isinstance(body, dict) else None
            if not saves:
                self._send(400, {"error": "格式不对：缺少 saves 字段"})
                return
            n = 0
            for d in saves:
                key = d.get("key")
                if not key:
                    continue
                with open(_save_path(key), "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=1)
                SESSIONS.pop(key, None)        # 下次访问时从文件重新加载
                n += 1
            self._send(200, {"ok": True, "imported": n})
            return

        self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8200)
    args = ap.parse_args()

    if not CLIENT.ready():
        print("⚠️  没检测到 OPENAI_API_KEY，接口调用会失败。")
        print("   在 assistant_agent/.env 里配好 key 再启动，或用 python providers.py 选模型。")

    n = migrate_saves()
    if n:
        print(f"已把旧存档合并为 {n} 份（每个角色一份）")

    threading.Thread(target=_ambient_loop, daemon=True).start()
    print("时间/天气：后台每 10 分钟刷新一次")

    if os.getenv("WHISPER_WARM", "1") != "0":
        def _warm():
            try:
                if _whisper_model():
                    print("[语音] 模型已就绪，可以通话")
            except Exception as e:
                print(f"[语音] 预热失败：{e}")
        threading.Thread(target=_warm, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"服务已启动：http://{args.host}:{args.port}")
    print(f"模型：{CLIENT.model} @ {CLIENT.base_url}")
    print("浏览器打开上面的地址即可聊天；游戏里 POST /chat 调用。Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
