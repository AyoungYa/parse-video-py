"""图形验证码（人机校验）：发短信前必须先过一次。

为什么必须有它：短信接口按条计费，一个手机号被人用脚本反复提交，一天就能烧掉几百块，
还会把我们自己的签名打到运营商那边。手机号校验 / 60 秒间隔 / 每日上限都拦不住
「换着手机号刷」和「多 IP 并发」——只有机器答不出的图片题能从源头挡住。

设计要点：
  · 答案只存哈希（带挑战号加盐），明文不落库、不进日志、不返回给前端；
  · 一次一用：核销即作废，错 3 次也作废，逼着爬虫重新取图（OCR 成本被放大）；
  · 字形用去混淆字母表（去掉 0O1IL 这类），人不用猜、机器照样难认；
  · 图片在服务端渲染成 PNG，前端只拿到 base64 —— 答案不在 DOM 里，扒源码没用。

★ 不要用「算术题 / 文字题」代替：答案是纯文本，正则一抓就走，等于没有。
"""

from __future__ import annotations

import base64
import io
import math
import random
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import CaptchaChallenge

#: 去混淆字母表：没有 0 O 1 I L，避免用户看错；也没有易混的 S/5 之外的组合
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
LENGTH = 4

#: 图片显示尺寸（前端按这个尺寸摆放，服务端按 SCALE 倍渲染保证 retinal 清晰）
DISPLAY_W, DISPLAY_H = 168, 56
SCALE = 2

# 深色系字色：和平台绿色调一致，但不影响识别
_INK = [
    (18, 62, 50),
    (32, 76, 62),
    (51, 65, 85),
    (24, 55, 78),
    (77, 60, 34),
]

#: 优先用系统粗体（笔画更实，缩到 56px 高也不糊）；容器里没有就退回 Pillow 内置字体
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


class CaptchaError(Exception):
    """人机校验失败，视图层直接转 4xx。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _p(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


# ---- 对外接口 ----


def new_challenge(ip: str | None = None) -> dict:
    """签发一个挑战，返回 {captcha_id, image, expires_in, dev_code?}。"""
    ttl = _p("CAPTCHA_TTL_SECONDS", 120)
    now = timezone.now()

    answer = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
    # captcha_no 的 default 在实例化时就已生成，所以能一次 INSERT 写完（带盐哈希要用它）
    row = CaptchaChallenge(expires_at=now + timedelta(seconds=ttl), ip=ip)
    row.code_hash = CaptchaChallenge.hash_code(row.captcha_no, answer)
    row.save()

    _purge(now)

    resp = {
        "captcha_id": row.captcha_no,
        "image": _data_uri(answer),
        "expires_in": ttl,
    }
    if getattr(settings, "CAPTCHA_DEV_ECHO", False):
        # 仅本地联调：让自动化脚本能登录。生产必须为 0（.env.example 已默认 0）
        resp["dev_code"] = answer
    return resp


def verify_and_consume(captcha_id: str | None, answer: str | None) -> None:
    """核销挑战。任何异常都抛 CaptchaError，调用方只负责转响应。"""
    captcha_id = (captcha_id or "").strip()
    answer = (answer or "").strip()
    if not captcha_id or not answer:
        raise CaptchaError("captcha_required", "请先输入图形验证码")

    max_attempts = _p("CAPTCHA_MAX_ATTEMPTS", 3)
    row = CaptchaChallenge.objects.filter(captcha_no=captcha_id).first()
    if row is None:
        raise CaptchaError("captcha_not_found", "图形验证码已失效，请刷新后重试")
    if row.used_at is not None:
        # 重放：同一个挑战不能过第二次（前端每发一次都会换新图）
        raise CaptchaError("captcha_used", "图形验证码已使用，请刷新后重试")
    if timezone.now() > row.expires_at:
        raise CaptchaError("captcha_expired", "图形验证码已过期，请刷新后重试")
    if row.attempts >= max_attempts:
        raise CaptchaError("captcha_locked", "图形验证码错误次数过多，请刷新后重试")

    if CaptchaChallenge.hash_code(row.captcha_no, answer) != row.code_hash:
        row.attempts += 1
        row.save(update_fields=["attempts"])
        left = max(0, max_attempts - row.attempts)
        raise CaptchaError(
            "captcha_wrong",
            "图形验证码错误" if left else "图形验证码错误次数过多，请刷新后重试",
        )

    row.used_at = timezone.now()
    row.save(update_fields=["used_at"])


# ---- 内部 ----


def _purge(now) -> None:
    """顺手清掉一天前就结束的挑战，避免表无限增长（失败不影响签发）。"""
    horizon = now - timedelta(days=1)
    try:
        CaptchaChallenge.objects.filter(expires_at__lt=horizon).delete()
    except Exception:  # pragma: no cover - 清理是尽力而为
        pass


def _font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    # Pillow ≥10.1 的 load_default(size) 返回内置 TrueType 字体，容器里没有系统字体也能用
    return ImageFont.load_default(size=size)


def _data_uri(answer: str) -> str:
    raw = render_png(answer)
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def render_png(answer: str) -> bytes:
    """把答案画成 PNG 字节。留成公开函数，方便 scripts/ 里单独出图检查。"""
    from PIL import Image, ImageDraw

    w, h = DISPLAY_W * SCALE, DISPLAY_H * SCALE
    img = Image.new("RGB", (w, h), (246, 249, 247))
    draw = ImageDraw.Draw(img)

    # 背景：极淡的斜纹，纯色底容易被二值化后一刀切干净
    for i in range(0, w + h, 14 * SCALE):
        draw.line([(i, 0), (i - h, h)], fill=(240, 244, 242), width=SCALE)

    font = _font(int(15 * SCALE))
    slot = w // LENGTH
    for idx, ch in enumerate(answer):
        color = random.choice(_INK)
        angle = random.uniform(-16, 16)
        glyph = _glyph(ch, font, color, angle)
        # 字符在各自格子里随机上下浮动，避免基线太整齐
        cx = idx * slot + (slot - glyph.width) // 2
        cx += random.randint(-2 * SCALE, 2 * SCALE)
        cy = (h - glyph.height) // 2 + random.randint(-3 * SCALE, 3 * SCALE)
        img.paste(glyph, (cx, cy), glyph)

    # 干扰线：两条正弦曲线，压过字但不盖死
    for _ in range(2):
        amplitude = random.uniform(2, 5) * SCALE
        phase = random.uniform(0, math.pi * 2)
        y0 = random.uniform(0.3, 0.7) * h
        points = []
        for x in range(0, w, 3 * SCALE):
            y = y0 + amplitude * math.sin(x / (10.0 * SCALE) + phase)
            points.append((x, y))
        draw.line(
            points,
            fill=random.choice([(120, 150, 140), (150, 165, 190), (170, 150, 130)]),
            width=SCALE,
            joint="curve",
        )

    # 噪点：破坏连通域，OCR 预处理要多花一道
    for _ in range(160):
        x = random.randrange(w)
        y = random.randrange(h)
        r = random.choice([1, 1, 1, 2]) * SCALE
        draw.ellipse(
            [x, y, x + r, y + r],
            fill=random.choice([(210, 218, 214), (196, 205, 200)]),
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _glyph(ch: str, font, color: tuple[int, int, int], angle: float):
    """把单个字符画到透明图层再旋转，返回可直接 paste 的 RGBA 图。"""
    from PIL import Image, ImageDraw

    box = font.getbbox(ch)
    gw, gh = box[2] - box[0], box[3] - box[1]
    pad = 6 * SCALE
    layer = Image.new("RGBA", (gw + pad * 2, gh + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(
        (pad - box[0], pad - box[1]), ch, font=font, fill=color + (255,)
    )
    return layer.rotate(angle, resample=Image.BICUBIC, expand=False)
