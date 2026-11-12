#!/usr/bin/env python
"""检查阿里云号码认证短信服务配置；--to 会真实发送一条短信。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="短信认证服务体检")
    parser.add_argument("--to", help="真实发送一条验证码到此手机号（会计费）")
    args = parser.parse_args()

    provider = os.getenv("SMS_PROVIDER", "console")
    print(f"SMS_PROVIDER={provider}")
    if provider != "dypnsapi":
        print("请先配置 SMS_PROVIDER=dypnsapi；console 模式只供本地开发。")
        return 1

    names = (
        "SMS_ALIYUN_ACCESS_KEY_ID", "SMS_ALIYUN_ACCESS_KEY_SECRET",
        "SMS_ALIYUN_SIGN_NAME", "SMS_ALIYUN_TEMPLATE_CODE",
    )
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        print("缺少配置：" + "、".join(missing))
        return 1

    try:
        from alibabacloud_dypnsapi20170525.client import Client
        from alibabacloud_dypnsapi20170525.models import SendSmsVerifyCodeRequest
        from alibabacloud_tea_openapi.models import Config
    except ImportError:
        print("缺少号码认证 SDK，请更新项目依赖。")
        return 1

    client = Client(Config(
        access_key_id=os.environ["SMS_ALIYUN_ACCESS_KEY_ID"],
        access_key_secret=os.environ["SMS_ALIYUN_ACCESS_KEY_SECRET"],
        endpoint="dypnsapi.aliyuncs.com", connect_timeout=3000, read_timeout=5000,
    ))
    print("配置与 SDK 检查通过。")
    if not args.to:
        print("如需真实发送，可加 --to <手机号>；正常登录请从页面发码以保留本地记录。")
        return 0

    if not (len(args.to) == 11 and args.to.startswith("1") and args.to.isdigit()):
        print("手机号格式不正确。")
        return 1
    try:
        body = client.send_sms_verify_code(SendSmsVerifyCodeRequest(
            phone_number=args.to,
            sign_name=os.environ["SMS_ALIYUN_SIGN_NAME"],
            template_code=os.environ["SMS_ALIYUN_TEMPLATE_CODE"],
            template_param='{"code":"##code##","min":"5"}',
            code_length=6, code_type=1, valid_time=300, interval=60,
            return_verify_code=False,
        )).body
    except Exception as exc:
        print(f"发送失败：{type(exc).__name__}")
        return 1
    if getattr(body, "code", None) == "OK" and getattr(body, "success", False) is True:
        print("发送请求成功，请检查手机是否收到短信。")
        return 0
    print(f"发送失败：{getattr(body, 'code', 'UNKNOWN')}；RequestId={getattr(body, 'request_id', '')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
