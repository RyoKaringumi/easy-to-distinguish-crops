#!/usr/bin/env python3
"""easy-to-distinguish-crops のアセット生成スクリプト。

リポジトリ直下（pack.mcmeta と同じ階層）を出力先として、
  assets/minecraft/blockstates/*.json   … バニラの作物ブロックの見た目をこのパックのモデルへ差し替え
  assets/etdc/models/block/*.json       … 「棒＋キャップ」モデル
  assets/etdc/textures/block/*.png      … 生成テクスチャ（成熟は .png.mcmeta 付きで点滅）
  pack.png / preview/legend.png         … アイコンと凡例
を全部作り直す。Pillow だけに依存。

    python tools/gen.py

見た目のルール（README の凡例と一致させること）:
  未成熟 … 緑の棒。高さ＝成長の進み具合。棒の先端 2px と上面キャップの中心が作物色。
           白い数字は「収穫可能まであと何段階」。
  成熟   … 全面が作物色の箱＋白い✓。作物色⇄白で明滅（茎系は明滅しない）。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
NS = "etdc"

# pack.mcmeta の互換範囲（リソースパック形式の major）。1.21.11=75, 26.1=84, 26.2=88, 26.3=97。
# 新しい版で「古いバージョン向け」と警告が出たら MAX_FORMAT を上げて再生成する。
# min_format の major が 64 を超えるパックでは supported_formats を書くとエラーになり、pack_format は省略できる
# （26.2/26.3 の PackFormat.validate を逆アセンブルして確認、2026-09-21）。
MIN_FORMAT = 75
MAX_FORMAT = 97
DESCRIPTION = "Crops: growth stage & ripeness, easy to distinguish"

# 陰影を消して面の向きに関係なく同じ明るさにする。
# 26.2 までは "shade": false、26.3 からは "shade_direction_override": "up"（バニラ block/crop と同じ）。
# 未知のキーは無視されるので両方書いておけばどちらの版でも効く。
NO_SHADE = {"shade": False, "shade_direction_override": "up"}
MC_BS = ROOT / "assets" / "minecraft" / "blockstates"
MODELS = ROOT / "assets" / NS / "models" / "block"
TEX = ROOT / "assets" / NS / "textures" / "block"
PREVIEW = ROOT / "preview"

# ---- 色 -------------------------------------------------------------------
GREEN = (67, 190, 70, 255)       # 未成熟の棒の本体
GREEN_DK = (28, 110, 40, 255)    # 棒の輪郭
WHITE = (255, 255, 255, 255)
INK = (24, 24, 24, 255)          # 数字の縁取り
CLEAR = (0, 0, 0, 0)

# 作物ごとの定義。ages=age プロパティの取りうる数（最大 age = ages-1 が成熟）。
# pulse=成熟テクスチャを明滅させるか。motif=近い色相の作物を見分けるための模様。
CROPS: dict[str, dict] = {
    "wheat":            dict(ages=8, color=(255, 214, 10),  motif=None,       pulse=True,  label="小麦"),
    "carrots":          dict(ages=8, color=(255, 109, 0),   motif=None,       pulse=True,  label="ニンジン"),
    "potatoes":         dict(ages=8, color=(156, 79, 219),  motif=None,       pulse=True,  label="ジャガイモ"),
    "beetroots":        dict(ages=4, color=(214, 30, 60),   motif=None,       pulse=True,  label="ビートルート"),
    "nether_wart":      dict(ages=4, color=(41, 121, 255),  motif=None,       pulse=True,  label="ネザーウォート"),
    "sweet_berry_bush": dict(ages=4, color=(255, 105, 180), motif="dots",     pulse=True,  label="スイートベリー"),
    "melon_stem":       dict(ages=8, color=(0, 229, 255),   motif="vstripes", pulse=False, label="スイカの茎"),
    "pumpkin_stem":     dict(ages=8, color=(255, 150, 0),   motif="face",     pulse=False, label="カボチャの茎"),
    # トーチフラワーの苗は age 0,1 のみ。age 2 相当はトーチフラワー本体（バニラのまま）。
    "torchflower_crop": dict(ages=3, color=(255, 170, 60),  motif=None,       pulse=False, label="トーチフラワーの苗", only_ages=(0, 1)),
}
COCOA = dict(color=(139, 90, 43), label="カカオ")
PITCHER = dict(ages=5, color=(110, 200, 255), label="ウツボカズラの苗")

# ---- 3x5 ドットフォント / ✓ ---------------------------------------------
FONT = {
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "001", "001", "001"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
}
CHECK_L = [  # 12x9
    "..........11",
    ".........111",
    "........111.",
    ".......111..",
    "11....111...",
    "111..111....",
    ".111111.....",
    "..1111......",
    "...11.......",
]
CHECK_S = [  # 6x5
    ".....1",
    "....11",
    "1..11.",
    "1111..",
    ".11...",
]


def rgba(c, a=255):
    return (c[0], c[1], c[2], a)


def mix(c, d, t):
    return tuple(round(c[i] * (1 - t) + d[i] * t) for i in range(3))


def darker(c, f=0.62):
    return tuple(round(v * f) for v in c)


def luminance(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def ink_for(c):
    """明るい作物色には黒、暗い作物色には白の文字。"""
    return INK if luminance(c) > 150 else WHITE


def put(img, x, y, c):
    if 0 <= x < img.width and 0 <= y < img.height:
        img.putpixel((x, y), c)


def blit_bitmap(img, bitmap, x0, y0, color, outline=None):
    """bitmap（'1' が塗り）を (x0,y0) に描く。outline を与えると 1px 縁取り。"""
    if outline is not None:
        for y, row in enumerate(bitmap):
            for x, ch in enumerate(row):
                if ch == "1":
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            put(img, x0 + x + dx, y0 + y + dy, outline)
    for y, row in enumerate(bitmap):
        for x, ch in enumerate(row):
            if ch == "1":
                put(img, x0 + x, y0 + y, color)


def draw_digit(img, digit: int, cx: int, cy: int, color, outline=INK):
    """3x5 の数字を中心 (cx,cy) に。"""
    bm = FONT[str(digit)]
    blit_bitmap(img, bm, cx - 1, cy - 2, color, outline)


def rect(img, x0, y0, x1, y1, fill, outline=None):
    d = ImageDraw.Draw(img)
    d.rectangle([x0, y0, x1, y1], fill=fill, outline=outline)


def draw_motif(img, motif, x0, y0, x1, y1, color):
    """成熟テクスチャの背景模様（作物色の暗い版）。範囲は両端含む。"""
    if motif is None:
        return
    c = rgba(darker(color))
    w, h = x1 - x0 + 1, y1 - y0 + 1
    if motif == "vstripes":
        for x in range(x0, x1 + 1):
            if (x - x0) % 4 in (0, 1):
                for y in range(y0, y1 + 1):
                    put(img, x, y, c)
    elif motif == "dots":
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if (x - x0) % 4 in (1, 2) and (y - y0) % 4 in (1, 2):
                    put(img, x, y, c)
    elif motif == "face":
        # ジャック・オ・ランタン風: 目2つ＋ぎざぎざの口（16x16 前提、中央寄せ）
        eyes = [(3, 3), (4, 3), (3, 4), (11, 3), (12, 3), (12, 4)]
        mouth = [(3, 11), (4, 12), (5, 11), (6, 12), (7, 11), (8, 12), (9, 11), (10, 12), (11, 11), (12, 12)]
        for (x, y) in eyes + mouth:
            put(img, x0 + x, y0 + y, c)
    elif motif == "checker":
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if ((x - x0) // 4 + (y - y0) // 4) % 2 == 0:
                    put(img, x, y, c)


# ---- テクスチャ生成 -------------------------------------------------------
def bar_height(p: float) -> int:
    """未成熟の棒の高さ px。p=進捗 0..1（1 は成熟なので呼ばない）。"""
    return round(4 + 10 * p)


def cap_size(p: float) -> int:
    return round(6 + 6 * p)


def immature_side(color, h: int, remaining: int | None, width=8, x0=4) -> Image.Image:
    """未成熟の側面: 緑の棒（幅 width, 高さ h）。先端 2px が作物色。数字＝残り段階。"""
    img = Image.new("RGBA", (16, 16), CLEAR)
    x1 = x0 + width - 1
    y0 = 16 - h
    rect(img, x0, y0, x1, 15, fill=GREEN, outline=GREEN_DK)
    if h >= 5:
        rect(img, x0 + 1, y0 + 1, x1 - 1, y0 + 2, fill=rgba(color))
    if remaining is not None:
        if h >= 11:
            draw_digit(img, remaining, (x0 + x1) // 2, y0 + 6, WHITE, GREEN_DK)
        elif y0 >= 6:
            draw_digit(img, remaining, (x0 + x1) // 2, y0 - 4, WHITE, INK)
    return img


def immature_cap(color, s: int, remaining: int | None) -> Image.Image:
    """未成熟の上面: 中央に s×s の四角。外側が緑、中心が作物色。"""
    img = Image.new("RGBA", (16, 16), CLEAR)
    x0 = 8 - s // 2
    x1 = x0 + s - 1
    rect(img, x0, x0, x1, x1, fill=GREEN, outline=GREEN_DK)
    if s >= 6:
        rect(img, x0 + 2, x0 + 2, x1 - 2, x1 - 2, fill=rgba(color))
    if remaining is not None and s >= 9:
        draw_digit(img, remaining, 8 - (1 - s % 2), 8 - (1 - s % 2), ink_for(color), None)
    return img


def mature_face(color, motif, bright: float, check=True) -> Image.Image:
    """成熟の 1 面（側面・上面共通）: 白枠＋作物色＋模様＋白い✓。bright で白へ寄せる。"""
    base = mix(color, (255, 255, 255), bright)
    img = Image.new("RGBA", (16, 16), rgba(base))
    rect(img, 0, 0, 15, 15, fill=None, outline=WHITE)
    tmp = Image.new("RGBA", (16, 16), CLEAR)
    draw_motif(tmp, motif, 1, 1, 14, 14, color)
    # 模様も同じだけ白へ寄せる
    px = tmp.load()
    for y in range(16):
        for x in range(16):
            r, g, b, a = px[x, y]
            if a:
                img.putpixel((x, y), rgba(mix((r, g, b), (255, 255, 255), bright)))
    if check:
        blit_bitmap(img, CHECK_L, 2, 4, WHITE, rgba(darker(base, 0.5)))
    return img


def animated(frames: list[Image.Image]) -> Image.Image:
    """縦に連結（Minecraft のアニメーション形式）。"""
    out = Image.new("RGBA", (16, 16 * len(frames)), CLEAR)
    for i, f in enumerate(frames):
        out.paste(f, (0, 16 * i))
    return out


def save_tex(name: str, img: Image.Image, pulse: bool = False):
    TEX.mkdir(parents=True, exist_ok=True)
    img.save(TEX / f"{name}.png")
    meta = TEX / f"{name}.png.mcmeta"
    if pulse:
        meta.write_text(dumps({"animation": {"interpolate": True, "frametime": 10, "frames": [0, 1]}}))
    elif meta.exists():
        meta.unlink()


def mature_tex(name: str, color, motif, pulse: bool):
    a = mature_face(color, motif, 0.0)
    if pulse:
        b = mature_face(color, motif, 0.6)
        save_tex(name, animated([a, b]), pulse=True)
    else:
        save_tex(name, a)


# ---- モデル生成 -----------------------------------------------------------
def crop_planes(tex_ref: str, y0: float, y1: float):
    """バニラ block/crop と同じ # 配置の 4 枚板。"""
    def plane(frm, to, faces):
        return {"from": frm, "to": to, **NO_SHADE, "faces": faces}
    uv_n = [0, 0, 16, 16]
    uv_r = [16, 0, 0, 16]
    return [
        plane([4, y0, 0], [4, y1, 16], {"west": {"uv": uv_n, "texture": tex_ref}, "east": {"uv": uv_r, "texture": tex_ref}}),
        plane([12, y0, 0], [12, y1, 16], {"west": {"uv": uv_r, "texture": tex_ref}, "east": {"uv": uv_n, "texture": tex_ref}}),
        plane([0, y0, 4], [16, y1, 4], {"north": {"uv": uv_n, "texture": tex_ref}, "south": {"uv": uv_r, "texture": tex_ref}}),
        plane([0, y0, 12], [16, y1, 12], {"north": {"uv": uv_r, "texture": tex_ref}, "south": {"uv": uv_n, "texture": tex_ref}}),
    ]


def cap_plane(tex_ref: str, y: float):
    return {
        "from": [0, y, 0], "to": [16, y, 16], **NO_SHADE,
        "faces": {"up": {"uv": [0, 0, 16, 16], "texture": tex_ref}, "down": {"uv": [0, 0, 16, 16], "texture": tex_ref}},
    }


def dumps(obj) -> str:
    """indent=2 だが、数値だけの配列（座標・UV）は 1 行にまとめる。"""
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    return re.sub(r"\[\s+((?:-?\d+(?:\.\d+)?,?\s*)+)\]",
                  lambda m: "[" + ", ".join(t.rstrip(",") for t in m.group(1).split()) + "]", text) + "\n"


def write_model(name: str, model: dict):
    MODELS.mkdir(parents=True, exist_ok=True)
    (MODELS / f"{name}.json").write_text(dumps(model))


def bar_model(name: str, side_tex: str, cap_tex: str | None, cap_y: float | None, y0=-1, y1=15):
    model = {
        "ambientocclusion": False,
        "textures": {"particle": f"{NS}:block/{side_tex}", "side": f"{NS}:block/{side_tex}"},
        "elements": crop_planes("#side", y0, y1),
    }
    if cap_tex is not None:
        model["textures"]["cap"] = f"{NS}:block/{cap_tex}"
        model["elements"].append(cap_plane("#cap", cap_y))
    write_model(name, model)


def write_blockstate(block: str, variants: dict):
    MC_BS.mkdir(parents=True, exist_ok=True)
    (MC_BS / f"{block}.json").write_text(dumps({"variants": variants}))


# ---- 作物ごとの生成 -------------------------------------------------------
def gen_bar_crop(block: str, spec: dict):
    ages, color, motif, pulse = spec["ages"], spec["color"], spec["motif"], spec["pulse"]
    max_age = ages - 1
    variants = {}
    for age in spec.get("only_ages", range(ages)):
        p = age / max_age
        side = f"{block}_side_{age}"
        cap = f"{block}_cap_{age}"
        if age == max_age:
            mature_tex(side, color, motif, pulse)
            mature_tex(cap, color, motif, pulse)
            bar_model(f"{block}_{age}", side, cap, 15)
        else:
            h, s = bar_height(p), cap_size(p)
            remaining = max_age - age
            save_tex(side, immature_side(color, h, remaining))
            save_tex(cap, immature_cap(color, s, remaining))
            bar_model(f"{block}_{age}", side, cap, -1 + h)
        variants[f"age={age}"] = {"model": f"{NS}:block/{block}_{age}"}
    write_blockstate(block, variants)


def gen_cocoa():
    color = COCOA["color"]
    # バニラの形状（サヤの大きさが段階で変わる）をそのまま使い、テクスチャだけ差し替える
    geo = {
        0: dict(box=([6, 7, 11], [10, 12, 15]), top=[0, 0, 4, 4], side=[11, 4, 15, 9]),
        1: dict(box=([5, 5, 9], [11, 12, 15]), top=[0, 0, 6, 6], side=[9, 4, 15, 11]),
        2: dict(box=([4, 3, 7], [12, 12, 15]), top=[0, 0, 8, 8], side=[8, 4, 16, 13]),
    }
    variants = {}
    for age, g in geo.items():
        tex = f"cocoa_{age}"
        sx0, sy0, sx1, sy1 = g["side"][0], g["side"][1], g["side"][2] - 1, g["side"][3] - 1
        tx1 = g["top"][2] - 1
        if age == 2:
            frames = []
            for bright in (0.0, 0.6):
                base = mix(color, (255, 255, 255), bright)
                img = Image.new("RGBA", (16, 16), rgba(base))
                rect(img, sx0, sy0, sx1, sy1, fill=None, outline=WHITE)
                rect(img, 0, 0, tx1, tx1, fill=None, outline=WHITE)
                blit_bitmap(img, CHECK_S, sx0 + 1, sy0 + 2, WHITE, rgba(darker(base, 0.5)))
                blit_bitmap(img, CHECK_S, 1, 2, WHITE, rgba(darker(base, 0.5)))
                # 軸（stem）は緑のまま
                rect(img, 12, 0, 15, 3, fill=GREEN_DK)
                frames.append(img)
            save_tex(tex, animated(frames), pulse=True)
        else:
            img = Image.new("RGBA", (16, 16), GREEN)
            rect(img, sx0, sy0, sx1, sy1, fill=GREEN, outline=GREEN_DK)
            rect(img, sx0 + 1, sy0 + 1, sx1 - 1, sy0 + 1, fill=rgba(color))
            rect(img, 0, 0, tx1, tx1, fill=rgba(color), outline=GREEN_DK)
            rect(img, 12, 0, 15, 3, fill=GREEN_DK)
            if age == 1:
                draw_digit(img, 2 - age, (sx0 + sx1) // 2 + 1, sy0 + 5, WHITE, GREEN_DK)
            save_tex(tex, img)
        frm, to = g["box"]
        model = {
            "ambientocclusion": False,
            "textures": {"particle": f"{NS}:block/{tex}", "cocoa": f"{NS}:block/{tex}"},
            "elements": [
                {"from": frm, "to": to, **NO_SHADE, "faces": {
                    "up": {"uv": g["top"], "texture": "#cocoa"},
                    "down": {"uv": g["top"], "texture": "#cocoa"},
                    "north": {"uv": g["side"], "texture": "#cocoa"},
                    "south": {"uv": g["side"], "texture": "#cocoa"},
                    "west": {"uv": g["side"], "texture": "#cocoa"},
                    "east": {"uv": g["side"], "texture": "#cocoa"},
                }},
                {"from": [8, 12, 12], "to": [8, 16, 16], **NO_SHADE, "faces": {
                    "west": {"uv": [12, 0, 16, 4], "texture": "#cocoa"},
                    "east": {"uv": [16, 0, 12, 4], "texture": "#cocoa"},
                }},
            ],
        }
        write_model(f"cocoa_{age}", model)
        for facing, rot in (("south", 0), ("west", 90), ("north", 180), ("east", 270)):
            v = {"model": f"{NS}:block/cocoa_{age}"}
            if rot:
                v["y"] = rot
            variants[f"age={age},facing={facing}"] = v
    write_blockstate("cocoa", variants)


def gen_pitcher():
    """2 ブロック分の高さを 1 本の棒として扱う。lower は y=-1..15、upper は y=0..16。"""
    color = PITCHER["color"]
    ages = PITCHER["ages"]
    max_age = ages - 1
    variants = {}
    for age in range(ages):
        p = age / max_age
        remaining = max_age - age
        if age == max_age:
            mature_tex("pitcher_crop_side", color, "checker", True)
            mature_tex("pitcher_crop_cap", color, "checker", True)
            bar_model(f"pitcher_crop_lower_{age}", "pitcher_crop_side", None, None)
            bar_model(f"pitcher_crop_upper_{age}", "pitcher_crop_side", "pitcher_crop_cap", 16, y0=0, y1=16)
        else:
            H = round(4 + 28 * p)          # 2 ブロック合計の棒の高さ
            hl, hu = min(H, 16), max(H - 16, 0)
            s = cap_size(p)
            lower_side = f"pitcher_crop_lower_side_{age}"
            cap = f"pitcher_crop_cap_{age}"
            save_tex(cap, immature_cap(color, s, remaining))
            if hu == 0:
                save_tex(lower_side, immature_side(color, hl, remaining))
                bar_model(f"pitcher_crop_lower_{age}", lower_side, cap, -1 + hl)
                write_model(f"pitcher_crop_upper_{age}", {"ambientocclusion": False, "textures": {"particle": f"{NS}:block/{cap}"}})
            else:
                save_tex(lower_side, immature_side(color, 16, None))
                upper_side = f"pitcher_crop_upper_side_{age}"
                save_tex(upper_side, immature_side(color, hu, remaining))
                bar_model(f"pitcher_crop_lower_{age}", lower_side, None, None)
                bar_model(f"pitcher_crop_upper_{age}", upper_side, cap, hu, y0=0, y1=16)
        variants[f"age={age},half=lower"] = {"model": f"{NS}:block/pitcher_crop_lower_{age}"}
        variants[f"age={age},half=upper"] = {"model": f"{NS}:block/pitcher_crop_upper_{age}"}
    write_blockstate("pitcher_crop", variants)


# ---- アイコンと凡例 -------------------------------------------------------
def gen_pack_icon():
    img = Image.new("RGBA", (16, 16), (60, 40, 20, 255))
    img.paste(immature_side((255, 214, 10), 9, None, width=4, x0=1), (0, 0), immature_side((255, 214, 10), 9, None, width=4, x0=1))
    m = mature_face((255, 214, 10), None, 0.0)
    img.paste(m.crop((0, 0, 10, 16)).resize((10, 16)), (6, 0))
    img.resize((64, 64), Image.NEAREST).save(ROOT / "pack.png")


def gen_legend():
    """preview/legend.png: 各作物の側面・上面テクスチャを段階順に並べた凡例（4 倍拡大）。"""
    PREVIEW.mkdir(exist_ok=True)
    rows = []
    for block, spec in CROPS.items():
        ages = spec.get("only_ages", range(spec["ages"]))
        rows.append((spec["label"], block, [(f"{block}_side_{a}", f"{block}_cap_{a}") for a in ages]))
    rows.append((COCOA["label"], "cocoa", [(f"cocoa_{a}", None) for a in range(3)]))
    rows.append((PITCHER["label"], "pitcher_crop", [
        (f"pitcher_crop_lower_side_{a}" if a < 4 else "pitcher_crop_side",
         f"pitcher_crop_cap_{a}" if a < 4 else "pitcher_crop_cap") for a in range(5)]))
    scale = 4
    cell = 16 * scale + 6
    width = 210 + cell * 8
    height = (cell * 2 + 22) * len(rows) + 10
    sheet = Image.new("RGBA", (width, height), (40, 40, 40, 255))
    d = ImageDraw.Draw(sheet)
    y = 6
    for label, block, cells in rows:
        d.text((8, y + cell - 6), f"{block}", fill=(230, 230, 230, 255))
        d.text((8, y + cell + 8), "side / cap", fill=(160, 160, 160, 255))
        for i, (side, cap) in enumerate(cells):
            x = 210 + i * cell
            for j, name in enumerate((side, cap)):
                if name is None:
                    continue
                im = Image.open(TEX / f"{name}.png").convert("RGBA").crop((0, 0, 16, 16))
                sheet.paste(im.resize((16 * scale, 16 * scale), Image.NEAREST), (x, y + j * cell), im.resize((16 * scale, 16 * scale), Image.NEAREST))
            d.text((x, y + 2 * cell + 2), f"age {i}", fill=(200, 200, 200, 255))
        y += cell * 2 + 22
    sheet.save(PREVIEW / "legend.png")


def gen_pack_mcmeta():
    """min_format/max_format は単一整数で書く（max を [97, 0] のような配列にすると 97.1 の 26.3 が「古い」扱いになる）。"""
    (ROOT / "pack.mcmeta").write_text(dumps({"pack": {
        "description": DESCRIPTION,
        "min_format": MIN_FORMAT,
        "max_format": MAX_FORMAT,
    }}))


def main():
    for p in (MC_BS, MODELS, TEX):
        if p.exists():
            shutil.rmtree(p)
    gen_pack_mcmeta()
    for block, spec in CROPS.items():
        gen_bar_crop(block, spec)
    gen_cocoa()
    gen_pitcher()
    gen_pack_icon()
    gen_legend()
    n_tex = len(list(TEX.glob("*.png")))
    n_models = len(list(MODELS.glob("*.json")))
    n_bs = len(list(MC_BS.glob("*.json")))
    print(f"textures={n_tex} models={n_models} blockstates={n_bs}")


if __name__ == "__main__":
    main()
