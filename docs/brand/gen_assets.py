#!/usr/bin/env python
"""再生成全套品牌静态资产（「帧 · 擦」标）。

产出 frontend/public/：
  favicon.svg / logo-mark.svg / logo-mark-inverse.svg   —— 直接生成
  logo.svg / logo-reverse.svg                           —— 保留既有字标轮廓，仅替换图形标 mask
  favicon.png (32, 透明底) / apple-touch-icon.png (180, 全出血) —— playwright 截 SVG

用法：
    ~/Library/Application\\ Support/parse-video-py/lint-venv/bin/python docs/brand/gen_assets.py
（需要 playwright；字标轮廓数据见 _lockup_data.json，改字形先更新该文件与现有 logo*.svg）
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUB = ROOT / "cloud_web" / "frontend" / "public"
TMP = Path("/tmp/wj-brand-gen")
TMP.mkdir(exist_ok=True)

BRAND = "#0e5c4a"
PAPER = "#faf9f5"

# mask 内的三个挖空形：播放键（圆角三角）/ 右上弧形擦口 / 四角星芒
MARK_INNER = (
    '<path d="M21.5 23 L37.5 32 L21.5 41 Z" fill="#000" stroke="#000" '
    'stroke-width="3" stroke-linejoin="round"/>'
    '<path d="M60 4 L46 4 A14 14 0 0 0 60 18 Z" fill="#000"/>'
    '<path d="M49 18.5 L50.7 24.3 L56.5 26 L50.7 27.7 L49 33.5 L47.3 27.7 '
    'L41.5 26 L47.3 24.3 Z" fill="#000"/>'
)


def mark_svg(fill: str, mask_id: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'
        f"  <defs><mask id=\"{mask_id}\"><rect x=\"4\" y=\"4\" width=\"56\" height=\"56\" "
        'rx="13" fill="#fff"/>'
        f"{MARK_INNER}</mask></defs>\n"
        f'  <rect x="4" y="4" width="56" height="56" rx="13" fill="{fill}" mask="url(#{mask_id})"/>\n'
        "</svg>\n"
    )


def patch_lockup(path: Path, fill: str) -> None:
    """组合标：只换 mask 内形与印底色，字标轮廓原样保留。"""
    svg = path.read_text()
    svg = re.sub(r"<mask id=\"[^\"]+\">.*?</mask>", lambda m: m.group(0), svg)  # noqa: F841
    svg = re.sub(
        r"(<mask id=\"[^\"]+\"><rect x=\"4\" y=\"4\" width=\"56\" height=\"56\" rx=\"13\" fill=\"#fff\"/>).*?(</mask>)",
        rf"\g<1>{MARK_INNER}\g<2>",
        svg,
        flags=re.S,
    )
    # 印底色（mask 之后紧跟的第一个 rect）
    svg = re.sub(
        r'(<rect x="4" y="4" width="56" height="56" rx="13" fill=")[^"]+(" mask=)',
        rf"\g<1>{fill}\g<2>",
        svg,
    )
    path.write_text(svg)
    print(f"patched {path.name}")


def main() -> None:
    (PUB / "favicon.svg").write_text(mark_svg(BRAND, "wj-fav"))
    (PUB / "logo-mark.svg").write_text(mark_svg(BRAND, "wj-mark"))
    (PUB / "logo-mark-inverse.svg").write_text(mark_svg(PAPER, "wj-mark-inv"))
    print("wrote favicon.svg / logo-mark.svg / logo-mark-inverse.svg")

    patch_lockup(PUB / "logo.svg", BRAND)
    patch_lockup(PUB / "logo-reverse.svg", PAPER)

    # ---- PNG：favicon 32 透明底；apple-touch-icon 180 全出血（iOS 自带圆角遮罩） ----
    full_bleed_inner = (
        '<path d="M17.5 22.5 L34.5 32 L17.5 41.5 Z" fill="#000" stroke="#000" '
        'stroke-width="3.2" stroke-linejoin="round"/>'
        '<path d="M64 0 L49 0 A15 15 0 0 0 64 15 Z" fill="#000"/>'
        '<path d="M50.5 19 L52.4 25.1 L58.5 27 L52.4 28.9 L50.5 35 L48.6 28.9 '
        'L42.5 27 L48.6 25.1 Z" fill="#000"/>'
    )
    bleed = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f"<defs><mask id=\"wj-bleed\"><rect width=\"64\" height=\"64\" fill=\"#fff\"/>{full_bleed_inner}</mask></defs>"
        f'<rect width="64" height="64" fill="{BRAND}" mask="url(#wj-bleed)"/>'
        "</svg>"
    )
    (TMP / "bleed.svg").write_text(bleed)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        # favicon.png：透明底
        p = b.new_page(viewport={"width": 32, "height": 32}, device_scale_factor=2)
        p.goto(f"file://{PUB}/favicon.svg")
        p.wait_for_timeout(200)
        p.screenshot(path=str(PUB / "favicon.png"), omit_background=True)
        p.close()
        # apple-touch-icon.png：全出血绿底
        p = b.new_page(viewport={"width": 180, "height": 180}, device_scale_factor=1)
        p.goto(f"file://{TMP}/bleed.svg")
        p.wait_for_timeout(200)
        p.screenshot(path=str(PUB / "apple-touch-icon.png"))
        b.close()
    print("wrote favicon.png / apple-touch-icon.png")


if __name__ == "__main__":
    main()
