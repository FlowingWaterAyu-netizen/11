#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成角色头像（SVG 占位图），每个角色 3 种表情。

生成到 web/avatars/：
    小助   assistant.svg / assistant-happy.svg / assistant-angry.svg
    老铁   blacksmith.svg / blacksmith-happy.svg / blacksmith-angry.svg
    王嫂   innkeeper.svg / ...
    老人   sage.svg / ...
    旅人   player.svg（只有中性，玩家不换表情）

不带后缀的是「中性」，带 -happy / -angry 的是另外两种表情。
以后有真美术图了，同名覆盖或改 personas.py 的 avatar 字段即可。
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "web", "avatars")
os.makedirs(OUT, exist_ok=True)

EMOTIONS = ["neutral", "happy", "angry"]


def write(filename, content):
    with open(os.path.join(OUT, filename), "w", encoding="utf-8") as f:
        f.write(content)


def wrap(bg, body):
    """统一的 200x200 头像外框：圆底 + 内容。"""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" width="200" height="200">
  <defs><clipPath id="c"><circle cx="100" cy="100" r="100"/></clipPath></defs>
  <g clip-path="url(#c)">
    <rect width="200" height="200" fill="{bg}"/>
    {body}
  </g>
</svg>
"""


def shoulders(cloth, y=138):
    return (f'<path d="M28,200 Q28,{y} 100,{y} Q172,{y} 172,200 Z" fill="{cloth}"/>')


# ---------------------------------------------------------------- 五官部件
def eyes(emo, cx1=84, cx2=116, cy=96, r=7, color="#22303f"):
    if emo == "happy":                       # 弯弯的笑眼
        return (f'<path d="M{cx1 - 9},{cy + 3} Q{cx1},{cy - 9} {cx1 + 9},{cy + 3}" '
                f'stroke="{color}" stroke-width="4" fill="none" stroke-linecap="round"/>'
                f'<path d="M{cx2 - 9},{cy + 3} Q{cx2},{cy - 9} {cx2 + 9},{cy + 3}" '
                f'stroke="{color}" stroke-width="4" fill="none" stroke-linecap="round"/>')
    return (f'<ellipse cx="{cx1}" cy="{cy}" rx="{r}" ry="{r + 2}" fill="{color}"/>'
            f'<ellipse cx="{cx2}" cy="{cy}" rx="{r}" ry="{r + 2}" fill="{color}"/>')


def brows(emo, cx1=84, cx2=116, cy=78, color="#3a2b22"):
    if emo == "angry":                       # 倒八字的怒眉
        return (f'<path d="M{cx1 - 13},{cy - 5} L{cx1 + 10},{cy + 9}" '
                f'stroke="{color}" stroke-width="5" stroke-linecap="round"/>'
                f'<path d="M{cx2 + 13},{cy - 5} L{cx2 - 10},{cy + 9}" '
                f'stroke="{color}" stroke-width="5" stroke-linecap="round"/>')
    return ""


def mouth(emo, cy=118, w=18, color="#b9645a"):
    if emo == "happy":                       # 大笑
        return (f'<path d="M{100 - w - 6},{cy - 2} Q100,{cy + 22} {100 + w + 6},{cy - 2}" '
                f'stroke="{color}" stroke-width="5" fill="none" stroke-linecap="round"/>')
    if emo == "angry":                       # 撇嘴
        return (f'<path d="M{100 - w},{cy + 8} Q100,{cy - 6} {100 + w},{cy + 8}" '
                f'stroke="{color}" stroke-width="5" fill="none" stroke-linecap="round"/>')
    return (f'<path d="M{100 - w},{cy} Q100,{cy + 12} {100 + w},{cy}" '
            f'stroke="{color}" stroke-width="4" fill="none" stroke-linecap="round"/>')


def blush(cx1=70, cx2=130, cy=112):
    return (f'<ellipse cx="{cx1}" cy="{cy}" rx="11" ry="7" fill="#e8697a" opacity="0.4"/>'
            f'<ellipse cx="{cx2}" cy="{cy}" rx="11" ry="7" fill="#e8697a" opacity="0.4"/>')


def anger_mark():
    """脑门上的青筋。"""
    return ('<g opacity="0.95">'
            '<path d="M152,46 l0,26 M139,59 l26,0" stroke="#e04b4b" '
            'stroke-width="7" stroke-linecap="round"/></g>')


def sparkles():
    """开心时的星星。"""
    return ('<g fill="#ffe066">'
            '<path d="M158,40 l4,11 11,4 -11,4 -4,11 -4,-11 -11,-4 11,-4 z"/>'
            '<path d="M38,58 l3,8 8,3 -8,3 -3,8 -3,-8 -8,-3 8,-3 z"/></g>')


def mood_extras(emo):
    if emo == "angry":
        return anger_mark()
    if emo == "happy":
        return sparkles()
    return ""


# ---------------------------------------------------------------- 各角色
def assistant(emo):
    if emo == "happy":
        leds = (f'<path d="M74,99 Q84,88 94,99" stroke="#5fd0ff" stroke-width="6" '
                f'fill="none" stroke-linecap="round"/>'
                f'<path d="M106,99 Q116,88 126,99" stroke="#5fd0ff" stroke-width="6" '
                f'fill="none" stroke-linecap="round"/>')
    else:
        leds = (f'<ellipse cx="84" cy="99" rx="9" ry="11" fill="#5fd0ff"/>'
                f'<ellipse cx="116" cy="99" rx="9" ry="11" fill="#5fd0ff"/>'
                f'<ellipse cx="86" cy="95" rx="3" ry="4" fill="#ffffff"/>'
                f'<ellipse cx="118" cy="95" rx="3" ry="4" fill="#ffffff"/>')
    brows_svg = (brows(emo, color="#dfe9f5") if emo == "angry" else "")
    mouth_svg = mouth(emo, cy=120, w=14, color="#5fd0ff")
    return f"""
  <rect x="46" y="52" width="108" height="96" rx="26" fill="#e8f0fb"/>
  <rect x="46" y="52" width="108" height="96" rx="26" fill="none" stroke="#9db8d6" stroke-width="3"/>
  <rect x="62" y="78" width="76" height="42" rx="18" fill="#2b3a4d"/>
  {leds}
  {brows_svg}
  <line x1="100" y1="52" x2="100" y2="30" stroke="#9db8d6" stroke-width="5"/>
  <circle cx="100" cy="26" r="9" fill="#4ea1ff"/>
  <rect x="30" y="88" width="14" height="26" rx="7" fill="#c8d6e8"/>
  <rect x="156" y="88" width="14" height="26" rx="7" fill="#c8d6e8"/>
  {mouth_svg}
  {shoulders("#c8d6e8", 158)}
  {mood_extras(emo)}
"""


def blacksmith(emo):
    return f"""
  {shoulders("#8a5a2b")}
  <circle cx="100" cy="92" r="44" fill="#e7b48c"/>
  <path d="M56,84 Q100,40 144,84 L144,70 Q100,30 56,70 Z" fill="#3a2b22"/>
  <path d="M56,82 Q100,58 144,82" stroke="#c8433a" stroke-width="14" fill="none" stroke-linecap="round"/>
  {eyes(emo)}
  {brows(emo)}
  {mouth(emo, cy=120, w=20)}
  <path d="M74,122 Q100,142 126,122 Q120,150 100,150 Q80,150 74,122 Z" fill="#4a3628"/>
  <path d="M60,150 L52,200 L148,200 L140,150 Q100,140 60,150 Z" fill="#6b4522"/>
  <rect x="150" y="150" width="12" height="46" rx="6" fill="#8a5a2b"/>
  <rect x="132" y="138" width="48" height="22" rx="6" fill="#8d949e"/>
  {mood_extras(emo)}
"""


def innkeeper(emo):
    return f"""
  {shoulders("#7a3b52")}
  <circle cx="100" cy="94" r="43" fill="#f0c19c"/>
  <path d="M55,92 Q55,44 100,44 Q145,44 145,92 Q138,66 100,62 Q62,66 55,92 Z" fill="#5b3a2a"/>
  <circle cx="100" cy="34" r="20" fill="#5b3a2a"/>
  {eyes(emo, r=6)}
  {brows(emo, color="#4a2f22")}
  {blush()}
  {mouth(emo, cy=116, w=16)}
  <path d="M62,152 L56,200 L144,200 L138,152 Q100,142 62,152 Z" fill="#f4e3d0"/>
  <ellipse cx="150" cy="176" rx="20" ry="16" fill="#c98a4b"/>
  <rect x="160" y="160" width="10" height="34" rx="5" fill="#c98a4b"/>
  {mood_extras(emo)}
"""


def sage(emo):
    return f"""
  {shoulders("#2f4a44")}
  <path d="M52,96 Q52,36 100,36 Q148,36 148,96 L148,104 Q100,92 52,104 Z" fill="#3f5250"/>
  <circle cx="100" cy="94" r="40" fill="#e3bd98"/>
  {eyes(emo, r=5, color="#2b3a3a")}
  {brows(emo, color="#dfe6e6")}
  <path d="M68,112 Q100,128 132,112 Q130,170 100,186 Q70,170 68,112 Z" fill="#dfe6e6"/>
  <path d="M100,130 Q100,170 100,186" stroke="#c7d0d0" stroke-width="3" fill="none"/>
  {mouth(emo, cy=118, w=14, color="#a3705f")}
  <path d="M60,150 L54,200 L146,200 L140,150 Q100,140 60,150 Z" fill="#2f4a44"/>
  <rect x="146" y="120" width="8" height="70" rx="4" fill="#8a6a3a"/>
  <circle cx="150" cy="118" r="10" fill="#cfe8ff" opacity="0.9"/>
  {mood_extras(emo)}
"""


def chiaya(emo):
    """御园千绫：黑色长直发 + 眼镜 + 八重齿。"""
    return f"""
  <path d="M46,102 Q46,28 100,28 Q154,28 154,102 L154,188 Q136,152 132,106
           L68,106 Q64,152 46,188 Z" fill="#1c1924"/>
  {shoulders("#3d3a52")}
  <circle cx="100" cy="94" r="43" fill="#f4d2b0"/>
  <path d="M55,92 Q55,38 100,38 Q145,38 145,92 Q139,62 100,58 Q61,62 55,92 Z" fill="#241f2b"/>
  {eyes(emo, r=6, color="#3a2f4a")}
  {brows(emo, color="#241f2b")}
  {blush()}
  {mouth(emo, cy=118, w=13, color="#c06a72")}
  <g stroke="#d6cfe4" stroke-width="3" fill="none" opacity="0.95">
    <rect x="63" y="83" width="35" height="28" rx="9"/>
    <rect x="102" y="83" width="35" height="28" rx="9"/>
    <path d="M98,96 L102,96"/>
    <path d="M57,92 L63,96"/>
    <path d="M137,96 L143,92"/>
  </g>
  {mood_extras(emo)}
"""


def monika(emo):
    """莫妮卡：棕色长发 + 侧马尾 + 白蝴蝶结 + 绿眼睛。"""
    return f"""
  <path d="M42,100 Q42,24 100,24 Q158,24 158,100 L158,194
           Q136,150 132,104 L68,104 Q64,150 42,194 Z" fill="#754b29"/>
  {shoulders("#2f2f3a")}
  <path d="M126,52 Q168,74 163,126 Q158,172 131,194 Q147,132 121,86 Z" fill="#8a5a34"/>
  <g transform="translate(154,90)">
    <path d="M0,0 L-24,-13 L-24,13 Z" fill="#f6f6f8"/>
    <path d="M0,0 L24,-13 L24,13 Z" fill="#f6f6f8"/>
    <circle cx="0" cy="0" r="7" fill="#dcdce6"/>
  </g>
  <circle cx="100" cy="94" r="43" fill="#f7d9bc"/>
  <path d="M55,92 Q55,32 100,32 Q145,32 145,92 Q139,58 100,54 Q61,58 55,92 Z" fill="#8a5a34"/>
  {eyes(emo, r=6, color="#3fae6a")}
  {brows(emo, color="#6b4326")}
  {blush()}
  {mouth(emo, cy=118, w=15, color="#c06a72")}
  {mood_extras(emo)}
"""


def hsin(emo):
    """心（心月狐）：白发、狐耳、暖金色眼睛。"""
    return f"""
  <path d="M42,100 Q42,24 100,24 Q158,24 158,100 L158,192
           Q136,150 132,104 L68,104 Q64,150 42,192 Z" fill="#dde1ec"/>
  {shoulders("#2b3550")}
  <circle cx="100" cy="94" r="43" fill="#f7dcc4"/>
  <path d="M66,52 L54,4 L98,34 Z" fill="#eef1f7"/>
  <path d="M73,46 L64,16 L90,34 Z" fill="#e8a8b8"/>
  <path d="M134,52 L146,4 L102,34 Z" fill="#eef1f7"/>
  <path d="M127,46 L136,16 L110,34 Z" fill="#e8a8b8"/>
  <path d="M55,92 Q55,30 100,30 Q145,30 145,92 Q139,56 100,52 Q61,56 55,92 Z" fill="#eef1f7"/>
  {eyes(emo, r=6, color="#e0a33c")}
  {brows(emo, color="#c9cede")}
  {blush()}
  {mouth(emo, cy=118, w=15, color="#c06a72")}
  {mood_extras(emo)}
"""


def nwnu(emo):
    """西北师范：黑发盘髻 + 眼镜 + 深红制服（教书先生气质）。"""
    return f"""
  <path d="M42,100 Q42,26 100,26 Q158,26 158,100 L158,188
           Q136,150 132,104 L68,104 Q64,150 42,188 Z" fill="#2a2320"/>
  {shoulders("#5a2b33")}
  <circle cx="100" cy="94" r="43" fill="#f7d6b8"/>
  <path d="M55,92 Q55,30 100,30 Q145,30 145,92 Q139,56 100,52 Q61,56 55,92 Z" fill="#332a24"/>
  <circle cx="100" cy="27" r="21" fill="#2a2320"/>
  <circle cx="93" cy="22" r="8" fill="#3d332c"/>
  {eyes(emo, r=6, color="#4a3a2c")}
  {brows(emo, color="#332a24")}
  {blush()}
  {mouth(emo, cy=118, w=14, color="#c06a72")}
  <g stroke="#d8d2e2" stroke-width="3" fill="none" opacity="0.95">
    <rect x="63" y="83" width="35" height="28" rx="9"/>
    <rect x="102" y="83" width="35" height="28" rx="9"/>
    <path d="M98,96 L102,96"/>
    <path d="M57,92 L63,96"/>
    <path d="M137,96 L143,92"/>
  </g>
  {mood_extras(emo)}
"""


def hunter(emo):
    """猎人：兽皮兜帽 + 短须 + 锐利眼神。"""
    return f"""
  {shoulders("#3f4a32")}
  <circle cx="100" cy="94" r="43" fill="#e2b184"/>
  <path d="M48,96 Q48,20 100,20 Q152,20 152,96
           Q146,54 100,50 Q54,54 48,96 Z" fill="#6b5236"/>
  <path d="M48,96 Q41,60 58,34 L86,56 Q56,66 48,96 Z" fill="#5a4429"/>
  <path d="M152,96 Q159,60 142,34 L114,56 Q144,66 152,96 Z" fill="#5a4429"/>
  {eyes(emo, r=6, color="#3a2f24")}
  {brows(emo, color="#3a2f24")}
  <path d="M70,116 Q100,132 130,116 Q128,152 100,156 Q72,152 70,116 Z"
        fill="#4a3828"/>
  {mouth(emo, cy=121, w=12, color="#a85c50")}
  {mood_extras(emo)}
"""


def player(emo):
    return f"""
  {shoulders("#3b6ea5")}
  <circle cx="100" cy="94" r="43" fill="#e9b98f"/>
  <path d="M56,88 Q56,42 100,42 Q144,42 144,88 Q136,60 100,58 Q64,60 56,88 Z" fill="#2f2a26"/>
  {eyes(emo, r=6)}
  {brows(emo)}
  {mouth(emo, cy=114, w=16)}
  <path d="M62,150 L56,200 L144,200 L138,150 Q100,140 62,150 Z" fill="#3b6ea5"/>
  <path d="M100,150 L100,200" stroke="#2c5478" stroke-width="4"/>
"""


# ---------------------------------------------------------------- 生成
BAKES = [
    ("assistant", assistant, "#1d3350"),
    ("blacksmith", blacksmith, "#3a2a1c"),
    ("innkeeper", innkeeper, "#2f3a52"),
    ("sage", sage, "#1c2b2a"),
    ("chiaya", chiaya, "#2a2436"),
    ("monika", monika, "#2b2320"),
    ("hsin", hsin, "#232b3f"),
    ("nwnu", nwnu, "#3a2029"),
    ("hunter", hunter, "#26301f"),
    ("player", player, "#22303f"),
]

count = 0
for name, fn, bg in BAKES:
    emotions = EMOTIONS if name != "player" else ["neutral"]
    for emo in emotions:
        suffix = "" if emo == "neutral" else f"-{emo}"
        write(f"{name}{suffix}.svg", wrap(bg, fn(emo)))
        count += 1

print(f"已生成 {count} 张头像 -> {OUT}")
