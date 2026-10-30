#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打包 / 恢复整个项目。

用法：
    python backup.py                     # 打包（不含 API key）
    python backup.py --with-key          # 连 .env 一起打（含密钥，注意保密）
    python backup.py --list              # 列出已有备份
    python backup.py --restore X.zip     # 先预览会覆盖哪些文件（不写入）
    python backup.py --restore X.zip --yes   # 真的恢复

也能从网页上点「📦 整包备份」直接下载。
"""

import argparse
import io
import json
import os
import zipfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(HERE, "backups")

# 打包时跳过这些东西
SKIP_DIRS = {"__pycache__", "backups", ".git", ".idea"}
SKIP_FILES = {".DS_Store", "backup.json"}
SKIP_EXT = {".pyc", ".pyo"}

# 打包的目录（整个目录都收）
INCLUDE_DIRS = ["web", "game_clients", "saves", "workspace", "voice_call"]

# 打包的散装文件
INCLUDE_FILES = ["characters.json", "settings.json", "erased_last.json",
                 "player.json", "relations.json", "world_events.json",
                 "notes.json", "groups.json", "battles.json", "npc_state.json",
                 "spells.json",
                 "requirements.txt", "README.md", ".env.example"]

FORMAT = "assistant_agent_backup"
VERSION = 3


# ================================================================ 打包
def _collect(with_key=False):
    """收集要打包的文件，返回 [(绝对路径, 压缩包内相对路径)]。"""
    out = []

    def add(path):
        rel = os.path.relpath(path, HERE)
        if rel.startswith(".."):
            return
        if set(rel.split(os.sep)) & SKIP_DIRS:
            return
        base = os.path.basename(path)
        if base in SKIP_FILES or os.path.splitext(base)[1] in SKIP_EXT:
            return
        out.append((path, rel))

    for name in sorted(os.listdir(HERE)):
        path = os.path.join(HERE, name)
        if os.path.isfile(path):
            if name.endswith(".py") or name in INCLUDE_FILES \
                    or (with_key and name == ".env"):
                add(path)
        elif os.path.isdir(path) and name in INCLUDE_DIRS:
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for f in sorted(files):
                    add(os.path.join(root, f))
    return out


def build_zip(with_key=False):
    """把项目打包成 zip 的字节流（不落盘）。"""
    files = _collect(with_key)
    manifest = {
        "format": FORMAT, "version": VERSION,
        "created": datetime.now().isoformat(timespec="seconds"),
        "with_key": bool(with_key),
        "files": len(files),
        "note": "解压到项目目录即可恢复；或用 python backup.py --restore 这个包",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, rel in files:
            z.write(path, rel)
        z.writestr("backup.json", json.dumps(manifest, ensure_ascii=False,
                                             indent=2))
    return buf.getvalue()


def save_backup(with_key=False):
    """打包并保存到 backups/，返回文件路径。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_DIR, f"agent-backup-{stamp}.zip")
    with open(path, "wb") as f:
        f.write(build_zip(with_key))
    return path


def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if fn.endswith(".zip"):
            p = os.path.join(BACKUP_DIR, fn)
            out.append((fn, os.path.getsize(p), p))
    return out


# ================================================================ 恢复
def _open_zip(src):
    """接受「文件路径」或「bytes」，统一返回一个 ZipFile。"""
    if isinstance(src, (bytes, bytearray)):
        return zipfile.ZipFile(io.BytesIO(src))
    return zipfile.ZipFile(src)


def inspect_backup(src):
    """看看这个包里有啥、会覆盖哪些现有文件。返回 [(相对路径, 是否已存在)]。"""
    actions = []
    with _open_zip(src) as z:
        for info in z.infolist():
            if info.is_dir() or info.filename in SKIP_FILES:
                continue
            rel = info.filename
            target = os.path.abspath(os.path.join(HERE, rel))
            if not target.startswith(os.path.abspath(HERE) + os.sep):
                continue                       # 防路径穿越
            actions.append((rel, os.path.exists(target)))
    return actions


def restore_backup(src, yes=False):
    """恢复备份。src 可以是 zip 路径或 bytes；yes=False 时只预览。"""
    actions = inspect_backup(src)
    if not yes:
        return actions, False

    base = os.path.abspath(HERE)
    with _open_zip(src) as z:
        for info in z.infolist():
            if info.is_dir() or info.filename in SKIP_FILES:
                continue
            rel = info.filename
            target = os.path.abspath(os.path.join(HERE, rel))
            if not target.startswith(base + os.sep):
                continue                       # 防路径穿越
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as fsrc, open(target, "wb") as fdst:
                fdst.write(fsrc.read())
    return actions, True


# ================================================================ 单角色包
# 一个小 zip：角色定义 + 头像 + 存档 —— 用来分享或单独备份一个角色
import personas

AVATAR_DIR = os.path.join(HERE, "web", "avatars")
SAVE_DIR = os.path.join(HERE, "saves")
CHAR_PACK_MARKER = "single-character.json"
CHAR_PACK_FORMAT = "assistant_agent_character"


def _safe_name(s):
    """和 server.py 的 _save_path 保持一致的转义规则。"""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)


def _save_filename(key):
    return _safe_name(f"npc|{key}") + ".json"


def _avatar_files(key):
    if not os.path.isdir(AVATAR_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(AVATAR_DIR)):
        stem = os.path.splitext(fn)[0]
        if stem == key or stem.startswith(key + "-"):
            out.append(fn)
    return out


def build_character_zip(key):
    """把一个角色打成小 zip：角色定义 + 头像 + 存档。"""
    npc = personas.NPC_PERSONAS.get(key)
    entry = next((e for e in personas.CHARACTERS if e.get("key") == key), None)
    if npc is None or entry is None:
        raise ValueError(f"没有叫「{key}」的角色")

    payload = {
        "format": CHAR_PACK_FORMAT, "version": 1,
        "key": key, "name": npc.get("name", key),
        "exported": datetime.now().isoformat(timespec="seconds"),
        "character": entry,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(CHAR_PACK_MARKER,
                   json.dumps(payload, ensure_ascii=False, indent=2))
        for fn in _avatar_files(key):
            z.write(os.path.join(AVATAR_DIR, fn), f"avatars/{fn}")
        save_file = os.path.join(SAVE_DIR, _save_filename(key))
        if os.path.exists(save_file):
            z.write(save_file, "save.json")
    return buf.getvalue()


def inspect_pack(src):
    """看一眼这个 zip 是「整包备份」还是「单角色包」。"""
    with _open_zip(src) as z:
        names = z.namelist()
        if CHAR_PACK_MARKER in names:
            try:
                p = json.loads(z.read(CHAR_PACK_MARKER).decode("utf-8"))
            except Exception:
                return {"type": "unknown"}
            key = p.get("key", "")
            return {"type": "character", "key": key,
                    "name": p.get("name", key),
                    "exists": key in personas.NPC_PERSONAS,
                    "has_save": "save.json" in names,
                    "files": len(names)}
        return {"type": "backup",
                "files": len([n for n in names if n not in SKIP_FILES])}


def import_character(src, mode="copy"):
    """导入单角色包。

    mode="copy"      → 同名时改名成 xxx-2 存进去（不覆盖）
    mode="overwrite" → 同名时直接替换掉旧的
    """
    with _open_zip(src) as z:
        payload = json.loads(z.read(CHAR_PACK_MARKER).decode("utf-8"))
        entry = dict(payload.get("character") or {})
        old_key = entry.get("key") or payload.get("key")
        if not old_key:
            raise ValueError("角色包里没有 key")

        new_key = old_key
        pos = None
        if old_key in personas.NPC_PERSONAS:
            if mode == "overwrite":
                for i, e in enumerate(personas.CHARACTERS):
                    if e.get("key") == old_key:
                        pos = i                     # 记住原位置，覆盖后放回去
                        break
                personas.remove_character(old_key)
            else:
                i = 2
                while f"{old_key}-{i}" in personas.NPC_PERSONAS:
                    i += 1
                new_key = f"{old_key}-{i}"
                # 名字也加个后缀，免得列表里两个同名角色分不清
                entry["name"] = f"{entry.get('name', old_key)} {i}"

        # 改名时：头像文件名、avatar 字段里的路径都要跟着改
        if new_key != old_key:
            entry["key"] = new_key
            av = entry.get("avatar")
            if av:
                entry["avatar"] = av.replace(old_key, new_key, 1)

        restored_avatars = 0
        os.makedirs(AVATAR_DIR, exist_ok=True)
        for name in z.namelist():
            if not name.startswith("avatars/") or name.endswith("/"):
                continue
            fn = os.path.basename(name)
            if new_key != old_key:
                fn = fn.replace(old_key, new_key, 1)
            with z.open(name) as fsrc, \
                    open(os.path.join(AVATAR_DIR, fn), "wb") as fdst:
                fdst.write(fsrc.read())
            restored_avatars += 1

        personas.insert_character(entry, pos)

        wrote_save = False
        if "save.json" in z.namelist():
            try:
                data = json.loads(z.read("save.json").decode("utf-8"))
                data["key"] = f"npc|{new_key}"
                os.makedirs(SAVE_DIR, exist_ok=True)
                with open(os.path.join(SAVE_DIR, _save_filename(new_key)),
                          "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                wrote_save = True
            except Exception:
                pass

        return {"key": new_key, "old_key": old_key,
                "renamed": new_key != old_key,
                "avatars": restored_avatars, "has_save": wrote_save}


# ================================================================ 命令行
def main():
    ap = argparse.ArgumentParser(description="整包备份 / 恢复")
    ap.add_argument("--with-key", action="store_true",
                    help="把 .env（含 API key）也打进去")
    ap.add_argument("--list", action="store_true", help="列出已有备份")
    ap.add_argument("--restore", metavar="ZIP", help="从备份恢复")
    ap.add_argument("--yes", action="store_true", help="恢复时真的写入")
    args = ap.parse_args()

    if args.list:
        items = list_backups()
        if not items:
            print("还没有任何备份。")
            return
        print("已有备份：")
        for fn, size, _ in items:
            print(f"  {fn}   {size / 1024:.0f} KB")
        return

    if args.restore:
        path = args.restore
        if not os.path.exists(path):
            print(f"找不到文件：{path}")
            return
        actions, done = restore_backup(path, yes=args.yes)
        print(f"包里共 {len(actions)} 个文件：")
        new = [a for a in actions if not a[1]]
        over = [a for a in actions if a[1]]
        print(f"  会新增 {len(new)} 个，覆盖 {len(over)} 个")
        for rel, exists in actions[:15]:
            print(f"    {'覆盖' if exists else '新增'}  {rel}")
        if len(actions) > 15:
            print(f"    …… 还有 {len(actions) - 15} 个")
        if done:
            print("\n✅ 已恢复。重启服务（python server.py）后生效。")
        else:
            print("\n（这是预览。确认无误后加 --yes 真的恢复）")
        return

    path = save_backup(with_key=args.with_key)
    size = os.path.getsize(path)
    print(f"✅ 已备份：{path}")
    print(f"   大小：{size / 1024:.0f} KB")
    if args.with_key:
        print("   ⚠️  包含 .env（里面有 API key），别随便发给别人")
    else:
        print("   （未包含 .env / API key；需要的话加 --with-key）")


if __name__ == "__main__":
    main()
