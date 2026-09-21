#!/usr/bin/env python3
"""パック内の参照整合性チェック。blockstate→model→texture が全部存在し、JSON/mcmeta が壊れていないことを確認する。

    python tools/validate.py
"""
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
errors: list[str] = []


def load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        errors.append(f"JSON 破損: {p}: {e}")
        return None


def res(ref: str, kind: str, ext: str) -> Path:
    ns, _, path = ref.partition(":")
    if not path:
        ns, path = "minecraft", ns
    return ASSETS / ns / kind / f"{path}{ext}"


def check_model(ref: str, seen: set):
    if ref in seen:
        return
    seen.add(ref)
    p = res(ref, "models", ".json")
    if not p.exists():
        errors.append(f"モデルが無い: {ref} ({p})")
        return
    m = load(p)
    if m is None:
        return
    if "parent" in m:
        check_model(m["parent"], seen)
    textures = m.get("textures", {})
    for k, v in textures.items():
        if v.startswith("#"):
            continue
        tp = res(v, "textures", ".png")
        if not tp.exists():
            errors.append(f"テクスチャが無い: {v} (from {ref})")
            continue
        meta = tp.with_suffix(".png.mcmeta")
        if meta.exists():
            a = load(meta)
            if a is not None:
                im = Image.open(tp)
                frames = a.get("animation", {}).get("frames")
                n = im.height // im.width
                if frames and max(frames) >= n:
                    errors.append(f"アニメのフレーム数が画像と合わない: {tp} frames={frames} image_frames={n}")
    for el in m.get("elements", []):
        for face in el.get("faces", {}).values():
            t = face.get("texture", "")
            if t.startswith("#") and t[1:] not in textures:
                errors.append(f"未定義のテクスチャ変数 {t} in {ref}")


def main() -> int:
    seen: set = set()
    bs_dir = ASSETS / "minecraft" / "blockstates"
    for bs in sorted(bs_dir.glob("*.json")):
        d = load(bs)
        if d is None:
            continue
        for key, v in d.get("variants", {}).items():
            vs = v if isinstance(v, list) else [v]
            for x in vs:
                check_model(x["model"], seen)
    mc = load(ROOT / "pack.mcmeta")
    if mc is not None and "pack" not in mc:
        errors.append("pack.mcmeta に pack が無い")
    for e in errors:
        print("NG", e)
    print(f"blockstates={len(list(bs_dir.glob('*.json')))} models_checked={len(seen)} errors={len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
