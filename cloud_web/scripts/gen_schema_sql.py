#!/usr/bin/env python3
"""生成规范化建表 SQL（cloud_web/deploy/init_mysql.sql）。

用法（先确保目标库已跑完 `manage.py migrate`，且为最新结构）：

    export MYSQL_HOST=127.0.0.1 MYSQL_PORT=3306 MYSQL_NAME=wuhenjiang \
           MYSQL_USER=xxx MYSQL_PASSWORD=xxx
    python scripts/gen_schema_sql.py

可选参数：--out <路径>、--dump-bin <mysqldump 路径>、--check（只自检不写文件）。

★ 为什么需要「后处理」：Django 内置模型（auth_group / auth_permission /
  django_admin_log / user 继承自 AbstractUser 的字段 / M2M 关联表）不允许我们改
  模型定义，它们自带外键、哈希索引名、且没有中文注释。业务表已在 models.py 里
  用 db_constraint=False + db_comment + 显式索引名解决；本脚本负责补齐框架表，
  输出一份**全库零外键、全字段有注释、索引命名统一**的 DDL。

  Django 运行时不依赖数据库外键：JOIN 不需要 FK 约束，on_delete=CASCADE 由 ORM
  在应用层模拟。所以去掉这些 CONSTRAINT 不影响功能，反而便于分库分表与在线 DDL。

后处理四步（幂等，对已合规的业务表不会产生变化）：
  1. 删除所有 FOREIGN KEY 约束行并修复尾逗号；
  2. 把不以 idx_/uniq_ 开头的索引名按「表+列」规则重命名；
  3. 给缺 COMMENT 的列补中文说明（COLUMN_COMMENTS 字典 + 通用 fallback）；
  4. 给缺 COMMENT 的表补中文说明。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "deploy" / "init_mysql.sql"

#: 表归属分组（决定脚本里的排布顺序，业务表在前）
OWNER = {
    "user": "accounts", "sms_code": "accounts",
    "auth_token": "accounts",
    "user_groups": "accounts", "user_user_permissions": "accounts",
    "task": "tasks", "task_event": "tasks", "worker_node": "tasks",
    "point_ledger": "billing", "sign_in_record": "billing", "subscription": "billing",
    "auth_group": "django.contrib.auth", "auth_group_permissions": "django.contrib.auth",
    "auth_permission": "django.contrib.auth",
    "django_content_type": "django.contrib.contenttypes",
    "django_admin_log": "django.contrib.admin",
    "django_session": "django.contrib.sessions",
    "django_migrations": "django",
}
GROUP_ORDER = ["accounts", "tasks", "billing", "django.contrib.auth",
               "django.contrib.contenttypes", "django.contrib.admin",
               "django.contrib.sessions", "django"]
GROUP_TITLE = {
    "accounts": "账户、令牌与短信验证码",
    "tasks": "任务、事件与 Worker 租约",
    "billing": "积分账本、签到与订阅",
    "django.contrib.auth": "Django 权限框架",
    "django.contrib.contenttypes": "Django ContentType",
    "django.contrib.admin": "Django Admin 日志",
    "django.contrib.sessions": "Django Session",
    "django": "Django 迁移记录",
}

#: 表注释（业务表由模型的 db_table_comment 生成，这里只补框架表）
TABLE_COMMENTS = {
    "auth_group": "Django 权限组（框架表，业务侧一般不用）",
    "auth_group_permissions": "权限组与权限的多对多关联（框架表）",
    "auth_permission": "Django 权限项（框架表）",
    "django_content_type": "Django 模型类型注册表（框架表）",
    "django_admin_log": "Django Admin 操作日志（框架表）",
    "django_session": "Django 服务端会话（框架表）",
    "django_migrations": "Django 迁移执行记录；初始化脚本会写入基线，勿手改",
    "user_groups": "用户与权限组的多对多关联（框架表）",
    "user_user_permissions": "用户与权限项的多对多关联（框架表）",
}

#: 列注释：("表", "列") -> 中文说明。("*" , "列") 是通用 fallback。
COLUMN_COMMENTS = {
    ("*", "id"): "内部自增主键，不对外暴露",
    ("*", "user_id"): "所属用户 ID（逻辑外键，无 DB 约束）",
    ("*", "group_id"): "权限组 ID（逻辑外键，无 DB 约束）",
    ("*", "permission_id"): "权限项 ID（逻辑外键，无 DB 约束）",
    ("*", "content_type_id"): "ContentType ID（逻辑外键，无 DB 约束）",
    ("user", "id"): "内部自增主键；对外请用 user_no",
    ("user", "password"): "Django 密码哈希（手机号登录，通常不用于校验）",
    ("user", "last_login"): "最近登录时间",
    ("user", "is_superuser"): "是否超级管理员",
    ("user", "username"): "Django 内部用户名；对外请用 user_no",
    ("user", "first_name"): "名（Django 内置字段，业务未使用）",
    ("user", "last_name"): "姓（Django 内置字段，业务未使用）",
    ("user", "email"): "邮箱（Django 内置字段，业务未使用）",
    ("user", "is_staff"): "是否可登录 Admin",
    ("user", "is_active"): "账号是否启用；软禁用用此位，不要删行",
    ("user", "date_joined"): "账号创建时间（Django 内置）",
    ("auth_group", "name"): "权限组名称，唯一",
    ("auth_group_permissions", "group_id"): "权限组 ID",
    ("auth_group_permissions", "permission_id"): "权限项 ID",
    ("auth_permission", "name"): "权限名称",
    ("auth_permission", "content_type_id"): "所属 ContentType ID",
    ("auth_permission", "codename"): "权限代号",
    ("django_content_type", "app_label"): "Django app 标签",
    ("django_content_type", "model"): "模型名（小写）",
    ("django_admin_log", "action_time"): "操作时间",
    ("django_admin_log", "object_id"): "被操作对象 ID（字符串）",
    ("django_admin_log", "object_repr"): "被操作对象的展示名",
    ("django_admin_log", "action_flag"): "操作类型：1 新增 / 2 修改 / 3 删除",
    ("django_admin_log", "change_message"): "变更摘要",
    ("django_admin_log", "content_type_id"): "对象类型 ID",
    ("django_admin_log", "user_id"): "操作者用户 ID",
    ("django_migrations", "app"): "Django app 名",
    ("django_migrations", "name"): "迁移文件名",
    ("django_migrations", "applied"): "应用时间",
    ("django_session", "session_key"): "会话主键（哈希串）",
    ("django_session", "session_data"): "序列化的会话数据",
    ("django_session", "expire_date"): "会话过期时间",
}


def _dump_bin(explicit: str | None) -> str:
    candidates = [explicit] if explicit else []
    candidates += [shutil.which("mysqldump"), "/usr/local/mysql/bin/mysqldump",
                   "/usr/bin/mysqldump", "/opt/homebrew/bin/mysqldump"]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise SystemExit("找不到 mysqldump，请用 --dump-bin 指定路径")


def _mysql_env() -> dict:
    """凭据走 MYSQL_PWD 环境变量，不出现在命令行 / 脚本产物里。"""
    env = os.environ.copy()
    pwd = os.environ.get("MYSQL_PASSWORD", "")
    if pwd:
        env["MYSQL_PWD"] = pwd
    return env


def _run_dump(dump_bin: str, args: list[str], tables: list[str] | None = None) -> str:
    host = os.environ.get("MYSQL_HOST", "127.0.0.1")
    port = os.environ.get("MYSQL_PORT", "3306")
    name = os.environ.get("MYSQL_NAME", "wuhenjiang")
    user = os.environ.get("MYSQL_USER", "root")
    cmd = [dump_bin, f"-h{host}", f"-P{port}", f"-u{user}", *args, name, *(tables or [])]
    r = subprocess.run(cmd, env=_mysql_env(), capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"mysqldump 失败：{r.stderr.strip()}")
    return r.stdout


def _comment_for(table: str, col: str) -> str:
    return COLUMN_COMMENTS.get((table, col)) or COLUMN_COMMENTS.get(("*", col)) or f"{col}（框架字段）"


def _new_index_name(table: str, unique: bool, old: str, cols_text: str) -> str:
    if old.startswith(("idx_", "uniq_")):
        return old
    cols = re.findall(r"`([^`]+)`", cols_text)
    short = "_".join(c[:-3] if c.endswith("_id") else c for c in cols)
    name = f"{'uniq' if unique else 'idx'}_{table}_{short}"
    if len(name) > 60:  # MySQL 索引名上限 64
        name = name[:54] + "_" + hashlib.md5(name.encode()).hexdigest()[:5]
    return name


def normalize_table(stmt: str) -> tuple[str, int]:
    """规范化单条 CREATE TABLE，返回 (新语句, 删掉的外键数)。"""
    m = re.match(r"CREATE TABLE `([^`]+)`", stmt)
    table = m.group(1)
    lines = stmt.rstrip().splitlines()

    # ① 去外键
    fk_re = re.compile(r"^\s*CONSTRAINT\s+`[^`]+`\s+FOREIGN KEY\b")
    n_fk = sum(1 for ln in lines if fk_re.match(ln))
    kept = [ln for ln in lines if not fk_re.match(ln)]
    if n_fk:
        for i, ln in enumerate(kept):
            if ln.startswith(") ENGINE"):
                j = i - 1
                while j >= 0 and not kept[j].strip():
                    j -= 1
                if kept[j].rstrip().endswith(","):
                    kept[j] = kept[j].rstrip()[:-1]
                break

    # ② 索引改名（按表作用域，避免同名的 `name` 串表）
    out = []
    key_re = re.compile(r"^(  )(UNIQUE )?KEY `([^`]+)`(\s*\([^;]*\))(,?)$")
    for ln in kept:
        km = key_re.match(ln)
        if km:
            indent, uniq, old, cols_text, comma = km.groups()
            new = _new_index_name(table, bool(uniq), old, cols_text)
            ln = f"{indent}{'UNIQUE ' if uniq else ''}KEY `{new}`{cols_text}{comma}"
        out.append(ln)

    # ③ 列注释
    col_re = re.compile(r"^(  `([^`]+)`\s+.+?)(,?)$")
    out2 = []
    for ln in out:
        cm = col_re.match(ln)
        if cm and "COMMENT " not in ln:
            body, col, comma = cm.groups()
            text = _comment_for(table, col).replace("'", "''")
            ln = f"{body.rstrip()} COMMENT '{text}'{comma}"
        out2.append(ln)

    # ④ 表注释
    result = []
    for ln in out2:
        if ln.startswith(") ENGINE") and "COMMENT=" not in ln and table in TABLE_COMMENTS:
            text = TABLE_COMMENTS[table].replace("'", "''")
            ln = ln.rstrip(";") + f" COMMENT='{text}';"
        result.append(ln)

    return "\n".join(result), n_fk


def self_check(sql: str) -> list[str]:
    problems = []
    # 只看 DDL 行：注释里出现 "FOREIGN KEY" 字样不算违规
    ddl_only = "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))
    if "FOREIGN KEY" in ddl_only:
        problems.append("仍存在 FOREIGN KEY")
    for name in re.findall(r"(?:UNIQUE )?KEY `([^`]+)`", sql):
        if not name.startswith(("idx_", "uniq_")):
            problems.append(f"索引名不合规: {name}")
    for stmt in re.split(r"(?=^CREATE TABLE )", sql, flags=re.M):
        tm = re.match(r"CREATE TABLE `([^`]+)`", stmt)
        if not tm:
            continue
        t = tm.group(1)
        if "COMMENT=" not in stmt:
            problems.append(f"表 {t} 缺注释")
        for ln in stmt.splitlines():
            cm = re.match(r"^  `([^`]+)`\s+", ln)
            if cm and "COMMENT " not in ln:
                problems.append(f"列 {t}.{cm.group(1)} 缺注释")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="导出规范化 MySQL 建表 SQL")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--dump-bin", default=None)
    ap.add_argument("--check", action="store_true", help="只自检，不写文件")
    args = ap.parse_args()

    dump_bin = _dump_bin(args.dump_bin)
    common = ["--no-data", "--skip-add-drop-table", "--skip-comments",
              "--skip-add-locks", "--skip-disable-keys", "--skip-set-charset",
              "--skip-tz-utc"]
    raw = _run_dump(dump_bin, common)

    clean = [ln for ln in raw.splitlines()
             if ln.strip() and not ln.startswith("/*!")
             and not re.match(r"^\s*SET character_set_client", ln)]
    body = re.sub(r" AUTO_INCREMENT=\d+", "", "\n".join(clean))

    blocks, order = {}, []
    total_fk = 0
    for chunk in re.split(r"(?=^CREATE TABLE )", body, flags=re.M):
        m = re.match(r"CREATE TABLE `([^`]+)`", chunk)
        if not m:
            continue
        norm, n_fk = normalize_table(chunk)
        blocks[m.group(1)] = norm.rstrip()
        order.append(m.group(1))
        total_fk += n_fk

    # 迁移基线：让 Django 认为迁移已应用，避免首次 migrate 重复建表
    ins_raw = _run_dump(dump_bin, ["--no-create-info", "--complete-insert",
                                   "--skip-add-locks", "--skip-comments",
                                   "--skip-set-charset", "--skip-tz-utc"],
                        tables=["django_migrations"])
    inserts = [ln for ln in ins_raw.splitlines() if ln.startswith("INSERT INTO")]
    baseline = "\n".join(inserts)
    # 每个 INSERT 行里 N 条记录有 N-1 个 "),(" 分隔
    n_migrations = sum(ln.count("),(") + 1 for ln in inserts if ln.strip())

    parts = []
    for g in GROUP_ORDER:
        names = [n for n in order if OWNER.get(n) == g]
        if not names:
            continue
        parts.append(f"\n-- ======== {GROUP_TITLE[g]}  [{g}] ========")
        for n in names:
            parts.append(f"\n-- {n}")
            parts.append(blocks[n])

    header = f"""-- ============================================================
-- 无痕匠 cloud_web · MySQL 8 建表脚本（全量 {len(order)} 张表）
--
-- 生成：python scripts/gen_schema_sql.py（对空库 migrate 后导出真实结构，再规范化）
-- 环境：Django 5.2.19 / MySQL 8.0 / utf8mb4_unicode_ci
-- 规范：零外键、表与字段全中文 COMMENT、索引 idx_/uniq_ 命名、业务表带唯一业务号
-- 适用：★ 全新空库 ★ 已有数据的库请勿直接执行（脚本不含 DROP，会报表已存在）
--
-- 用法：
--   mysql -h <host> -P 3306 -u <user> -p < init_mysql.sql
--   RDS 用户：可先在控制台建库建号，然后只执行「USE」以下的建表部分。
--
-- 账号请自行创建，不要把密码写进脚本：
--   CREATE USER 'wuhenjiang'@'%' IDENTIFIED BY '<强密码>';
--   GRANT ALL PRIVILEGES ON `wuhenjiang`.* TO 'wuhenjiang'@'%';
--
-- ★ 零外键：业务表用 db_constraint=False；Django 内置表的 CONSTRAINT 由生成脚本去掉。
--   ORM 的 JOIN 不需要 FK 约束，on_delete=CASCADE 由 Django 在应用层模拟，功能不受影响。
-- ★ 末尾写入 {n_migrations} 条 django_migrations 基线，执行后跑 `migrate` 不会重复建表。
-- ============================================================

CREATE DATABASE IF NOT EXISTS `wuhenjiang`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `wuhenjiang`;

SET NAMES utf8mb4;
"""
    footer = f"""

-- ======== Django 迁移基线（{n_migrations} 条）========
-- 不写这段，首次 `manage.py migrate` 会尝试重建所有表并报 "table already exists"。
{baseline};

-- ======== 自检 ========
-- SELECT TABLE_NAME FROM information_schema.TABLES
--  WHERE TABLE_SCHEMA = 'wuhenjiang' ORDER BY TABLE_NAME;   -- 应为 {len(order)} 行
-- SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
--  WHERE TABLE_SCHEMA = 'wuhenjiang' AND CONSTRAINT_TYPE = 'FOREIGN KEY';  -- 应为 0
"""
    sql = header + "\n".join(parts) + footer

    problems = self_check(sql)
    print(f"表 {len(order)} 张 | 删除外键 {total_fk} 个 | 迁移基线 {n_migrations} 条")
    if problems:
        print("❌ 自检未通过：")
        for p in problems[:20]:
            print("   -", p)
        return 1
    print("✅ 自检通过：零外键 / 索引命名合规 / 表与列注释齐全")

    if args.check:
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(sql, encoding="utf-8")
    print(f"已写入 {out}（{len(sql.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
