"""让 PyMySQL 顶替 MySQLdb，避免生产环境编译 mysqlclient。"""

import pymysql

pymysql.install_as_MySQLdb()
