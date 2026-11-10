#!/usr/bin/env python
"""阿里云短信通道体检。

用途：.env 填完 SMS_ALIYUN_* 之后先跑这个，别直接上线等用户告诉你收不到码。
用法：
    uv run python scripts/check_sms.py            # 只体检，不真发
    uv run python scripts/check_sms.py --to 138xxxxxxxx   # 额外真发一条（计费！）

检查项：
  1. SMS_PROVIDER / 回显开关配置是否符合预期（生产必须 console 回显全关）
  2. AK / SK / 签名 / 模板四项是否齐全
  3. SDK 是否已安装（pyproject 里已带，环境异常时才会缺）
  4. 构造 Client（本地不联网，仅验证配置形态）
  5. --to 时真实发送一条登录验证码并打印阿里云返回码
     （成功 OK；失败时给出常见错误码的人话解释）

本脚本不写数据库：验证码只发不存，不影响 SmsCode 限流统计。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
except ImportError:
    print("缺少 python-dotenv，请先执行：uv sync")
    sys.exit(2)

load_dotenv(BASE_DIR / ".env", override=False)

import os  # noqa: E402  （必须在 load_dotenv 之后读环境变量）

PASS, WARN, FAIL = "✅", "⚠️ ", "❌"
results: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = PASS if ok else FAIL
    print(f"  {mark} {name}" + (f" —— {detail}" if detail else ""))
    results.append((name, "ok" if ok else "fail"))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="阿里云短信通道体检")
    parser.add_argument("--to", help="真实发送一条验证码到该手机号（会计费）")
    args = parser.parse_args()

    provider = os.getenv("SMS_PROVIDER", "console")
    ak = os.getenv("SMS_ALIYUN_ACCESS_KEY_ID", "")
    sk = os.getenv("SMS_ALIYUN_ACCESS_KEY_SECRET", "")
    sign = os.getenv("SMS_ALIYUN_SIGN_NAME", "")
    template = os.getenv("SMS_ALIYUN_TEMPLATE_CODE", "")

    print(f"SMS_PROVIDER = {provider}")

    print("\n[1] 配置完整性")
    if provider == "console":
        echo = os.getenv("SMS_DEV_ECHO", "0") == "1"
        check("console 通道（本地联调）", True)
        check(
            "SMS_DEV_ECHO 回显开关",
            True,
            "开（本地正常；★ 上生产必须改 0）" if echo else "关",
        )
        if not (ak and sk and sign and template):
            print(f"  {WARN} 阿里云四项配置未填全，切 aliyun 前先补齐")
            return 0
    else:
        check("provider=aliyun", True)
        check("SMS_ALIYUN_ACCESS_KEY_ID", bool(ak))
        check("SMS_ALIYUN_ACCESS_KEY_SECRET", bool(sk))
        check("SMS_ALIYUN_SIGN_NAME（需已备案通过）", bool(sign), sign or "")
        check("SMS_ALIYUN_TEMPLATE_CODE（需审核通过）", bool(template), template or "")
        if not (ak and sk and sign and template):
            return 1

    print("\n[2] SDK 与客户端")
    try:
        from alibabacloud_dysmsapi20170525.client import Client
        from alibabacloud_tea_openapi.models import Config

        check("SDK 可导入（alibabacloud-dysmsapi20170525）", True)
    except ImportError as e:
        check("SDK 可导入", False, f"{e}；执行 uv sync")
        return 1

    client = Client(
        Config(
            access_key_id=ak,
            access_key_secret=sk,
            endpoint="dysmsapi.aliyuncs.com",
            connect_timeout=3000,
            read_timeout=5000,
        )
    )
    check("Client 构造成功（endpoint=dysmsapi.aliyuncs.com）", True)

    if not args.to:
        print("\n未指定 --to，跳过真实发送。验证签名/模板是否真的可用：")
        print(f"  uv run python scripts/{Path(__file__).name} --to <手机号>")
        return 0

    print(f"\n[3] 真发一条到 {args.to}（会计费）")
    from alibabacloud_dysmsapi20170525.models import SendSmsRequest

    code = f"{__import__('secrets').randbelow(1000000):06d}"
    try:
        resp = client.send_sms(
            SendSmsRequest(
                phone_numbers=args.to,
                sign_name=sign,
                template_code=template,
                template_param=f'{{"code":"{code}"}}',
            )
        )
    except Exception as e:
        check("调用 dysmsapi", False, f"网络/鉴权异常：{e}")
        return 1

    body = resp.body
    biz_code = getattr(body, "code", "") or ""
    if biz_code == "OK":
        check("发送成功", True, f"验证码 {code}（ biz_id={getattr(body, 'biz_id', '')}）")
        print("\n  手机收到后，把这条验证码输进登录页即可完成端到端验证。")
        return 0

    hints = {
        "isv.BUSINESS_LIMIT_CONTROL": "触发阿里云流控（1 分钟/1 小时/1 天级），稍后再试",
        "isv.SMS_SIGNATURE_ILLEGAL": "签名不存在或未备案——去短信服务控制台核对签名名称（要一字不差）",
        "isv.SMS_TEMPLATE_ILLEGAL": "模板不存在或未审核通过——核对模板 CODE",
        "isv.TEMPLATE_MISSING_PARAMETERS": "模板变量与代码传参不符（模板要用 ${code}）",
        "isv.INVALID_ACCESS_KEY_ID": "AccessKey ID 不存在——核对 RAM 子账号",
        "SignatureDoesNotMatch": "AccessKey Secret 不对——重新复制",
        "isv.AMOUNT_NOT_ENOUGH": "账户余额不足，去充值",
        "isv.OUT_OF_SERVICE": "短信服务欠费停机，去控制台恢复",
        "isv.PRODUCT_UN_SUBSCRIPT": "阿里云账号未开通短信服务",
        "isv.MOBILE_NUMBER_ILLEGAL": "手机号格式不对",
    }
    detail = (
        f"code={biz_code} message={getattr(body, 'message', '')} "
        f"request_id={getattr(body, 'request_id', '')}\n"
        f"     {WARN} 解释：{hints.get(biz_code, '查阿里云短信错误码文档')}"
    )
    check("发送成功", False, detail)
    return 1


if __name__ == "__main__":
    sys.exit(main())
