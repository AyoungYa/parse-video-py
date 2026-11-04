#!/usr/bin/env python
"""远程 MySQL 连接体检。

用途：.env 填完先跑这个，比直接 migrate 只看到一个 2003/1045 好排查。
用法：
    uv run python scripts/check_db.py

检查项：
  1. 能不能连上（网络 / 防火墙 / 账号密码）
  2. 库存在不存在、字符集是不是 utf8mb4
  3. 连接是否走了 TLS（公网连接必须加密）
  4. 有没有全局 CREATE 权限（跑 Django 测试要建 test_ 库，没有会直接失败）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
except ImportError:
    print("缺少 python-dotenv，请先执行：uv sync")
    sys.exit(2)

load_dotenv(BASE_DIR / ".env", override=False)

try:
    import pymysql
except ImportError:
    print("缺少 PyMySQL，请先执行：uv sync")
    sys.exit(2)


def main() -> int:
    host = os.getenv("MYSQL_HOST", "")
    port = int(os.getenv("MYSQL_PORT", "3306"))
    name = os.getenv("MYSQL_NAME", "")
    user = os.getenv("MYSQL_USER", "")
    password = os.getenv("MYSQL_PASSWORD", "")

    if not host:
        print("⚠️  .env 里没有 MYSQL_HOST。先在 cloud_web/.env 填远程实例地址（模板见 .env.example）")
        print(f"   当前读到：user={user!r} db={name!r} port={port}")
        return 2
    if not password:
        print("⚠️  .env 里没有 MYSQL_PASSWORD。")
        return 2

    print(f"目标：{user}@{host}:{port}/{name}")

    ssl_args: dict[str, object] = {}
    if os.getenv("MYSQL_SSL") == "1":
        ssl_args["ssl"] = {"ca": os.environ["MYSQL_SSL_CA"]} if os.getenv("MYSQL_SSL_CA") else {}
        print(f"TLS：开启（{'校验证书' if os.getenv('MYSQL_SSL_CA') else '不校验证书'}）")
    else:
        print("TLS：关闭（走公网务必设 MYSQL_SSL=1）")

    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=name,
            connect_timeout=int(os.getenv("MYSQL_CONNECT_TIMEOUT", "10")),
            charset="utf8mb4",
            **ssl_args,
        )
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else 0
        detail = exc.args[1] if len(exc.args) > 1 else str(exc)
        print(f"\n❌ 连接失败（{code}）：{detail}")
        hints = {
            1045: "账号或密码不对。注意 'user'@'1.2.3.4' 和 'user'@'%' 是两个不同账号。",
            1049: f"库不存在。先建：CREATE DATABASE `{name}` CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci;",
            2003: "连不上端口。查实例是否运行、安全组/防火墙是否放行 3306、本机公网 IP 是否在允许名单。",
            1130: "该主机不允许连接。给账号补 host：CREATE USER 'u'@'%' 或指定当前公网 IP。",
            1044: f"账号对 `{name}` 没权限。GRANT ALL ON `{name}`.* TO 'u'@'host';",
        }
        print("   → " + hints.get(code, "查 MySQL 错误码文档，或看实例侧错误日志。"))
        return 1
    except Exception as exc:  # noqa: BLE001 网络异常种类多，统一报出
        print(f"\n❌ 连接失败：{type(exc).__name__}: {exc}")
        return 1

    ok = True
    with conn.cursor() as cur:
        cur.execute("SELECT VERSION(), @@character_set_database, @@collation_database")
        version, charset, collation = cur.fetchone()
        print(f"\n✅ 已连接  MySQL {version}")

        cur.execute("SHOW STATUS LIKE 'Ssl_cipher'")
        cipher = (cur.fetchone() or ("", ""))[1]
        tls_ok = bool(cipher)
        print(f"   传输加密：{cipher or '未加密'}", end="")
        if os.getenv("MYSQL_SSL") == "1" and not tls_ok:
            print("  ⚠️ 配了 SSL=1 但服务端没协商上，检查实例是否开启 require_secure_transport")
        else:
            print()

        print(f"   库字符集：{charset} / {collation}", end="")
        if charset != "utf8mb4":
            print("  ⚠️ 不是 utf8mb4，中文/emoji 会出问题")
            ok = False
        else:
            print("  ✅")

        cur.execute("SHOW GRANTS")
        grants = [row[0] for row in cur.fetchall()]
        print("\n账号权限：")
        for g in grants:
            print(f"   {g}")

        # 用 information_schema 的全局权限判断「能不能建库」，比正则猜 GRANTS 文本可靠。
        cur.execute("SELECT CURRENT_USER()")
        current_user = cur.fetchone()[0]
        grantee = "'" + current_user.replace("@", "'@'") + "'"
        cur.execute(
            "SELECT PRIVILEGE_TYPE FROM information_schema.USER_PRIVILEGES WHERE GRANTEE = %s",
            (grantee,),
        )
        global_privs = {row[0].upper() for row in cur.fetchall()}
        can_createdb = bool(global_privs & {"ALL PRIVILEGES", "CREATE"})
        has_db_level = any(not g.upper().startswith("GRANT USAGE") and f"`{name}`" in g for g in grants)

        print(f"\n   migrate 所需（库级 CREATE/ALTER/INDEX）：{'✅ 有库级授权' if has_db_level else '⚠️ 未见库级授权，核对上面的 GRANTS'}")
        if can_createdb:
            print("   跑测试所需（全局 CREATE DATABASE）：✅ 够，Django 能建 test_ 库")
        else:
            print("   跑测试所需（全局 CREATE DATABASE）：❌ 不足 —— migrate 能过，但 test 会报权限错")
            print("   → 补一句：GRANT CREATE ON *.* TO '{}'; 或单独授权 test 库".format(current_user))
            ok = False

    conn.close()
    print("\n下一步：uv run python manage.py migrate")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
