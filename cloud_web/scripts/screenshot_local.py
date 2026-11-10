#!/usr/bin/env python
"""本地页面效果截图（走真登录流程）。

用途：本地起好 dev server + vite 后，脚本自动登录并把关键页面截下来，
用来确认「页面效果是否符合预期」，不用手点。

前置：
  1. Django dev server 在 127.0.0.1:8000（config.settings.dev）
  2. Vite 在 127.0.0.1:5173
  3. SMS_PROVIDER=console（本地默认，验证码只打日志不真发）
  4. SMS_DEV_ECHO=1 且 CAPTCHA_DEV_ECHO=1（.env 本地默认已开）：
     登录页会回显「本地开发验证码 / 本地开发图形码」，脚本靠它自动填答案。

跑法：
    python scripts/screenshot_local.py
输出：
    docs/product/ui-screenshots/local-*.png
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5173"
OUT = Path(__file__).resolve().parent.parent.parent / "docs/product/ui-screenshots"
PHONE = "139" + str(int(time.time()))[-8:]


def shoot(page, name: str, full: bool = True) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"local-{name}.png"
    page.screenshot(path=str(path), full_page=full)
    print(f"  📸 {path}")


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        # ── 1. 登录页 ──────────────────────────────────────────────
        print("→ 打开登录页")
        page.goto(f"{BASE}/login", wait_until="networkidle")
        shoot(page, "01-login")

        # ── 2. 发送验证码并登录 ─────────────────────────────────────
        # ★ 图形验证码闸门：发送短信前必须先答图形码。
        #   本地 .env 开了 CAPTCHA_DEV_ECHO，登录页有「本地开发图形码 XXXX 填入」提示，
        #   脚本从这里拿答案（生产没有这条提示，脚本也就不该这么跑）。
        print(f"→ 发送验证码（手机号 {PHONE}）")
        page.fill('input[type="tel"], input[placeholder*="手机"]', PHONE)

        cap_code = None
        try:
            m = re.search(r"本地开发图形码[:：]\s*([A-Z2-9]{4})", page.inner_text("body"))
            cap_code = m.group(1) if m else None
        except Exception:
            pass
        if cap_code:
            print(f"→ 图形验证码 {cap_code}（开发回显）")
            page.fill('input[placeholder="4 位字符"]', cap_code)
        else:
            print("⚠️  页面没有图形码回显（CAPTCHA_DEV_ECHO 未开），跳过发送。")
            browser.close()
            return 0

        # ★ 精确匹配发送按钮：图形验证码的「换一张」按钮可访问名也含「验证码」，
        #   模糊正则会先点到它，导致验证码永远发不出去（2026-11-09 实测踩过）
        send_btn = page.get_by_role("button", name=re.compile("获取验证码|重发"))
        send_btn.first.click()
        time.sleep(2)
        shoot(page, "02-code-sent")

        # console 通道：验证码回显在页面提示里（SMS_DEV_ECHO=1）。
        code = None
        try:
            body = page.inner_text("body")
            m = re.search(r"本地开发验证码[:：]\s*(\d{6})", body)
            if m:
                code = m.group(1)
        except Exception:
            pass

        if not code:
            print("⚠️  页面没有回显验证码（本地 SMS_DEV_ECHO 未开）。")
            print("    请在 Django 日志里找到 [SMS:console] ... 发送验证码 XXXXXX")
            print("    然后手动在浏览器完成登录，后续页面请自行截图。")
            browser.close()
            return 0

        print(f"→ 用验证码 {code} 登录")
        # ★ 用 placeholder 精确锁定短信码输入框（input[type=text] 会先命中图形码框）
        page.fill('input[placeholder="6 位验证码"]', code)
        page.get_by_role("button", name=re.compile("登录|登 录")).first.click()
        # 登录成功后前端跳到 "/"（工作台）；等 URL 离开 /login 即可
        page.wait_for_url(re.compile(r"^(?!.*/login).*$"), timeout=15000)
        time.sleep(3)

        # ── 3. 工作台 ─────────────────────────────────────────────
        print("→ 工作台")
        shoot(page, "03-workbench")

        # ── 4. 关于页（备案信息）───────────────────────────────────
        print("→ 关于页")
        page.goto(f"{BASE}/about", wait_until="networkidle")
        shoot(page, "04-about")

        browser.close()
    print(f"\n✅ 截图完成 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
