"""对外业务号生成。

★ 为什么需要它：数据库自增 ID / UUID 是内部实现细节，不应暴露给用户端。
  每张业务表都有一个 `xxx_no`（varchar(32)、全局唯一、不可变），前端与开放 API
  只用它做查询 / 读写；以后换主键、分库分表、迁移到别的实现都不会打破外部契约。

格式：<1 位实体前缀><8 位日期><12 位大写随机 hex>，例：U20261105A1B2C3D4E5F6
  · 前缀让人一眼看出是什么对象，排障时扫日志很省事；
  · 日期便于按号粗略定位时间（不替代 created_at）；
  · 48 bit 随机 space 足够大，配合数据库唯一约束兜底。

★ 这些函数被 migration 以「路径引用」序列化（default=apps.bizno.gen_xxx_no）。
  不要改名、不要移动模块、不要删函数；要改格式请新增函数并改模型 default。
"""

from __future__ import annotations

import secrets
from datetime import datetime

#: 实体前缀表（改这里等于改对外契约，务必全链路同步）
PREFIX = {
    "user": "U",
    "sms_code": "C",
    "captcha": "P",  # P = picture challenge
    "auth_token": "K",
    "worker_node": "W",
    "task": "T",
    "task_event": "E",
    "point_ledger": "L",
    "sign_in_record": "S",
    "subscription": "B",
    "payment_order": "O",
    "point_lot": "G",
    "point_spend": "D",
}

#: 字段长度，所有模型的 *_no 字段统一用它，避免各表不一致
NO_MAX_LENGTH = 32


def _gen(entity: str) -> str:
    return (
        PREFIX[entity]
        + datetime.now().strftime("%Y%m%d")
        + secrets.token_hex(6).upper()
    )


def gen_user_no() -> str:
    return _gen("user")


def gen_sms_code_no() -> str:
    return _gen("sms_code")


def gen_captcha_no() -> str:
    return _gen("captcha")


def gen_auth_token_no() -> str:
    return _gen("auth_token")


def gen_worker_no() -> str:
    return _gen("worker_node")


def gen_task_no() -> str:
    return _gen("task")


def gen_task_event_no() -> str:
    return _gen("task_event")


def gen_ledger_no() -> str:
    return _gen("point_ledger")


def gen_sign_in_no() -> str:
    return _gen("sign_in_record")


def gen_subscription_no() -> str:
    return _gen("subscription")


def gen_payment_order_no() -> str:
    return _gen("payment_order")


def gen_point_lot_no() -> str:
    return _gen("point_lot")


def gen_point_spend_no() -> str:
    return _gen("point_spend")


def backfill_unique_nos(app_label: str, specs):
    """生成数据迁移用的 RunPython 回调，给历史行逐行补唯一业务号。

    ★ 为什么必须有：Django 给「已有数据的表」加带 default 的非空字段时，
      default callable **只调用一次**，所有旧行会拿到同一个值；紧接着建
      UniqueConstraint 必定报 duplicate entry。所以 AddField 之后、AddConstraint
      之前要逐行重刷一遍。

    specs: [(model_name, field_name, gen_func), ...]
    小表实现（一次取全部 pk）；表超过十万行时改成分批 iterator + bulk_update。
    """
    def _run(apps, schema_editor):
        db = schema_editor.connection.alias
        for model_name, field_name, gen in specs:
            model = apps.get_model(app_label, model_name)
            qs = model.objects.using(db)
            pks = list(qs.values_list("pk", flat=True))
            for pk in pks:
                qs.filter(pk=pk).update(**{field_name: gen()})

    return _run
