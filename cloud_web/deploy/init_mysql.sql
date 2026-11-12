-- ============================================================
-- 无痕匠 cloud_web · MySQL 8 建表脚本（全量 67 张表）
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
-- ★ 末尾写入 27 条 django_migrations 基线，执行后跑 `migrate` 不会重复建表。
-- ============================================================

CREATE DATABASE IF NOT EXISTS `wuhenjiang`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `wuhenjiang`;

SET NAMES utf8mb4;

-- ======== 账户、令牌与短信验证码  [accounts] ========

-- auth_token
CREATE TABLE `auth_token` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `key_hash` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '令牌 SHA-256；明文只在签发时返回一次',
  `created_at` datetime(6) NOT NULL COMMENT '签发时间',
  `expires_at` datetime(6) NOT NULL COMMENT '过期时间（默认 7 天）',
  `last_used_at` datetime(6) DEFAULT NULL COMMENT '最近一次使用时间',
  `user_id` bigint NOT NULL COMMENT '所属用户 ID（逻辑外键，不建 DB 约束）',
  `token_no` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '对外记录号 K+日期+随机',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_auth_token_no` (`token_no`),
  UNIQUE KEY `uniq_auth_token_key_hash` (`key_hash`),
  KEY `idx_auth_token_user` (`user_id`),
  KEY `idx_auth_token_expires_at` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='访问令牌：只存哈希，默认 7 天过期';

-- sms_code
CREATE TABLE `sms_code` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `phone` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '接收验证码的手机号',
  `code_hash` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '验证码 SHA-256，明文不落库',
  `purpose` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '用途：login / reset 等',
  `created_at` datetime(6) NOT NULL COMMENT '签发时间',
  `expires_at` datetime(6) NOT NULL COMMENT '过期时间（签发后 5 分钟）',
  `used_at` datetime(6) DEFAULT NULL COMMENT '核销时间；非空表示已使用',
  `attempts` smallint unsigned NOT NULL COMMENT '校验尝试次数，达 5 次作废',
  `ip` char(39) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '请求来源 IP，用于限流与审计',
  `sms_no` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '对外记录号 C+日期+随机',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_sms_code_no` (`sms_no`),
  KEY `idx_sms_code_phone_created` (`phone`,`created_at` DESC),
  KEY `idx_sms_code_expires_at` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='短信验证码：只存哈希，5 分钟有效、单次核销、错 5 次作废';

-- user
CREATE TABLE `user` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键；对外请用 user_no',
  `password` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Django 密码哈希（手机号登录，通常不用于校验）',
  `last_login` datetime(6) DEFAULT NULL COMMENT '最近登录时间',
  `is_superuser` tinyint(1) NOT NULL COMMENT '是否超级管理员',
  `username` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Django 内部用户名；对外请用 user_no',
  `first_name` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '名（Django 内置字段，业务未使用）',
  `last_name` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '姓（Django 内置字段，业务未使用）',
  `email` varchar(254) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '邮箱（Django 内置字段，业务未使用）',
  `is_staff` tinyint(1) NOT NULL COMMENT '是否可登录 Admin',
  `is_active` tinyint(1) NOT NULL COMMENT '账号是否启用；软禁用用此位，不要删行',
  `date_joined` datetime(6) NOT NULL COMMENT '账号创建时间（Django 内置）',
  `phone` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '登录手机号，E.164 或国内 11 位；未绑定为 NULL',
  `wx_openid` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '微信 openid（预留），未绑定为 NULL',
  `nickname` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '用户昵称，可空',
  `avatar` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '头像 URL，可空',
  `user_no` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '对外用户号 U+日期+随机，API 查询用此值，不暴露自增 ID',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_user_username` (`username`),
  UNIQUE KEY `uniq_user_no` (`user_no`),
  UNIQUE KEY `uniq_user_phone` (`phone`),
  UNIQUE KEY `uniq_user_wx_openid` (`wx_openid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户账户：手机号登录，承载积分、任务、订阅的归属主体';

-- user_groups
CREATE TABLE `user_groups` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `user_id` bigint NOT NULL COMMENT '所属用户 ID（逻辑外键，无 DB 约束）',
  `group_id` int NOT NULL COMMENT '权限组 ID（逻辑外键，无 DB 约束）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_user_groups_user_group` (`user_id`,`group_id`),
  KEY `idx_user_groups_group` (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户与权限组的多对多关联（框架表）';

-- user_user_permissions
CREATE TABLE `user_user_permissions` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `user_id` bigint NOT NULL COMMENT '所属用户 ID（逻辑外键，无 DB 约束）',
  `permission_id` int NOT NULL COMMENT '权限项 ID（逻辑外键，无 DB 约束）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_user_user_permissions_user_permission` (`user_id`,`permission_id`),
  KEY `idx_user_user_permissions_permission` (`permission_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户与权限项的多对多关联（框架表）';

-- ======== 任务、事件与 Worker 租约  [tasks] ========

-- task
CREATE TABLE `task` (
  `id` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '内部主键 UUID；对外请用 task_no',
  `status` varchar(24) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '任务状态；worker_offline 为内部接力状态，用户端保持原阶段',
  `original_key` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '原片 OSS 对象键',
  `mask_key` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'mask 图 OSS 对象键',
  `result_key` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '成片 OSS 对象键',
  `report_key` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '验收报告 report.json 的 OSS 对象键',
  `duration_ms` bigint NOT NULL COMMENT '视频时长（毫秒）',
  `size_bytes` bigint NOT NULL COMMENT '原片字节数',
  `width` int NOT NULL COMMENT '画面宽度（像素）',
  `height` int NOT NULL COMMENT '画面高度（像素）',
  `fps` double NOT NULL COMMENT '帧率',
  `params` json NOT NULL COMMENT '处理参数；自动定位失败时存 bbox 与候选框',
  `checkpoints` json NOT NULL COMMENT '已上报的处理阶段及 OSS 产物；接力 worker 按此恢复',
  `metrics` json NOT NULL COMMENT '验收指标：残留/纹理/闪烁三项及 all_green',
  `lease_expires_at` datetime(6) DEFAULT NULL COMMENT '租约到期时间；超时由 reclaim_leases 打回队列',
  `attempt` int NOT NULL COMMENT '当前阶段连续领取次数；阶段检查点成功后重置',
  `progress` double NOT NULL COMMENT '处理进度 0~1',
  `points_cost` int NOT NULL COMMENT '本单预扣积分数',
  `refunded_at` datetime(6) DEFAULT NULL COMMENT '积分退还时间；非空表示已退款',
  `error_code` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '错误码，成功为空',
  `error_message` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '错误详情，含阶段输出尾部',
  `created_at` datetime(6) NOT NULL COMMENT '创建时间',
  `started_at` datetime(6) DEFAULT NULL COMMENT '首次开始处理时间',
  `finished_at` datetime(6) DEFAULT NULL COMMENT '终态时间',
  `user_id` bigint NOT NULL COMMENT '所属用户 ID（逻辑外键，不建 DB 约束）',
  `worker_id` bigint DEFAULT NULL COMMENT '当前持有者节点 ID（逻辑外键），未领取为 NULL',
  `task_no` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '对外任务号 T+日期+随机；用户端查询/读写一律用此值',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_task_no` (`task_no`),
  KEY `idx_task_status_created` (`status`,`created_at`),
  KEY `idx_task_user_created` (`user_id`,`created_at` DESC),
  KEY `idx_task_worker` (`worker_id`),
  KEY `idx_task_lease_expires_at` (`lease_expires_at`),
  KEY `idx_task_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='处理任务：一次去水印作业的全生命周期与验收结果';

-- task_event
CREATE TABLE `task_event` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `from_status` varchar(24) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '变更前状态；首次创建为空',
  `to_status` varchar(24) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '变更后状态',
  `actor` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '触发者：system / 用户名 / worker 名',
  `detail` json NOT NULL COMMENT '附加信息：错误码、重试标记、耗时等',
  `created_at` datetime(6) NOT NULL COMMENT '事件发生时间',
  `task_id` char(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '所属任务 ID（逻辑外键，不建 DB 约束）',
  `event_no` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '对外事件号 E+日期+随机',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_task_event_no` (`event_no`),
  KEY `idx_task_event_task_created` (`task_id`,`created_at` DESC),
  KEY `idx_task_event_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='任务状态变更审计流水：排查线上卡单全靠它';

-- worker_node
CREATE TABLE `worker_node` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `name` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '节点唯一名称，便于运维识别',
  `token_hash` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '节点令牌 SHA-256，明文仅创建时展示一次',
  `capabilities` json NOT NULL COMMENT '能力声明：设备、显存、支持的分辨率等',
  `status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '节点状态：online / offline / disabled',
  `max_concurrency` int NOT NULL COMMENT '同时可领取的任务数上限',
  `last_heartbeat_at` datetime(6) DEFAULT NULL COMMENT '最近进程级 alive 上报时间；超过 60 秒判离线',
  `created_at` datetime(6) NOT NULL COMMENT '节点创建时间',
  `worker_no` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '对外节点号 W+日期+随机',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_worker_node_no` (`worker_no`),
  UNIQUE KEY `uniq_worker_node_name` (`name`),
  UNIQUE KEY `uniq_worker_node_token_hash` (`token_hash`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='算力节点：出站领取任务的 worker，令牌只存哈希';

-- ======== 积分账本、签到与订阅  [billing] ========

-- point_ledger
CREATE TABLE `point_ledger` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `delta` int NOT NULL COMMENT '积分变动值，正数为入账、负数为扣减；只增不改',
  `balance_after` int NOT NULL COMMENT '本行写入后的余额快照，禁止用 UPDATE 覆盖余额',
  `reason` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '变动原因：initial/sign_in/streak/share/consume/refund/subscribe/expire',
  `ref_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '关联的任务号或订单号，可空',
  `note` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '备注，可空',
  `created_at` datetime(6) NOT NULL COMMENT '入账时间',
  `user_id` bigint NOT NULL COMMENT '所属用户 ID（逻辑外键，不建 DB 约束）',
  `ledger_no` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '对外流水号 L+日期+随机；对账与用户端查询用此值',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_point_ledger_no` (`ledger_no`),
  KEY `idx_point_ledger_user_created` (`user_id`,`created_at` DESC),
  KEY `idx_point_ledger_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='积分流水：只增不改的账本行，每行带余额快照';

-- point_lot
CREATE TABLE `point_lot` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `lot_no` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '对外积分批次号 G+日期+随机',
  `source` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '来源：initial/sign_in/streak/share/legacy/refund',
  `remaining` int unsigned NOT NULL COMMENT '本批次尚可使用的积分',
  `expires_at` datetime(6) DEFAULT NULL COMMENT '到期时间，NULL 表示不过期',
  `created_at` datetime(6) NOT NULL COMMENT '发放时间',
  `user_id` bigint NOT NULL COMMENT '所属用户 ID（逻辑外键）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_point_lot_no` (`lot_no`),
  KEY `idx_point_lot_user_expiry` (`user_id`,`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='积分批次：按到期时间先后消费，赠送积分到期回收';

-- point_spend
CREATE TABLE `point_spend` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `spend_no` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '对外积分扣减号 D+日期+随机',
  `task_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '关联任务内部 ID',
  `amount` int unsigned NOT NULL COMMENT '从该批次扣减的积分',
  `refunded` tinyint(1) NOT NULL COMMENT '该笔扣分是否已退回',
  `created_at` datetime(6) NOT NULL COMMENT '扣分时间',
  `lot_id` bigint NOT NULL COMMENT '被扣减的积分批次 ID（逻辑外键）',
  `user_id` bigint NOT NULL COMMENT '所属用户 ID（逻辑外键）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_point_spend_no` (`spend_no`),
  KEY `point_spend_lot_id` (`lot_id`),
  KEY `idx_point_spend_user_task` (`user_id`,`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='任务积分批次消费记录，支持失败后按原批次退还';

-- sign_in_record
CREATE TABLE `sign_in_record` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `sign_date` date NOT NULL COMMENT '签到自然日；与 user 组成唯一约束，一天只能签一次',
  `points` int NOT NULL COMMENT '本次实得积分（含连签奖励）',
  `streak` int NOT NULL COMMENT '截至本次的连续签到天数',
  `created_at` datetime(6) NOT NULL COMMENT '签到时间',
  `user_id` bigint NOT NULL COMMENT '所属用户 ID（逻辑外键，不建 DB 约束）',
  `sign_in_no` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '对外签到号 S+日期+随机',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_sign_in_record_no` (`sign_in_no`),
  UNIQUE KEY `uniq_sign_in_record_user_date` (`user_id`,`sign_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='签到记录：每用户每天至多一条，连续 7 天额外奖励';

-- subscription
CREATE TABLE `subscription` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `plan` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '套餐：monthly / yearly',
  `status` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '订阅状态：active / expired / cancelled',
  `started_at` datetime(6) NOT NULL COMMENT '生效时间',
  `expires_at` datetime(6) NOT NULL COMMENT '到期时间；到期后额度回落到积分制',
  `minutes_used` int NOT NULL COMMENT '当前计费周期已用分钟数',
  `minutes_limit` int NOT NULL COMMENT '月度分钟硬闸，默认取 MONTHLY_MINUTES_LIMIT',
  `created_at` datetime(6) NOT NULL COMMENT '订阅创建时间',
  `user_id` bigint NOT NULL COMMENT '所属用户 ID（逻辑外键，不建 DB 约束）',
  `subscription_no` varchar(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '对外订阅号 B+日期+随机；支付订单关联用此值',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_subscription_no` (`subscription_no`),
  KEY `idx_subscription_user_expires` (`user_id`,`expires_at` DESC),
  KEY `idx_subscription_expires_at` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='订阅：包月/包年状态机与月度分钟额度';

-- payment_order
CREATE TABLE `payment_order` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `order_no` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '微信支付商户订单号，唯一且不超过 32 字符',
  `plan` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '购买套餐，目前仅 monthly',
  `amount_fen` int unsigned NOT NULL COMMENT '支付金额，单位分，由服务端定价',
  `status` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '订单状态：pending / paid / closed',
  `code_url` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '微信 Native 支付二维码链接，仅待支付期间使用',
  `transaction_id` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '微信支付交易号，支付成功时唯一',
  `expires_at` datetime(6) NOT NULL COMMENT '扫码支付截止时间',
  `paid_at` datetime(6) DEFAULT NULL COMMENT '支付成功时间',
  `last_checked_at` datetime(6) DEFAULT NULL COMMENT '最近向微信查询状态时间，用于限频',
  `created_at` datetime(6) NOT NULL COMMENT '订单创建时间',
  `subscription_id` bigint DEFAULT NULL COMMENT '支付成功后开通的会员订阅 ID（逻辑外键）',
  `user_id` bigint NOT NULL COMMENT '购买用户 ID（逻辑外键）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `order_no` (`order_no`),
  UNIQUE KEY `transaction_id` (`transaction_id`),
  KEY `idx_payment_user_created` (`user_id`,`created_at` DESC),
  KEY `idx_payment_status_expiry` (`status`,`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='微信扫码购买会员订单，支付回调幂等开通订阅';

-- ======== Django 权限框架  [django.contrib.auth] ========

-- auth_group
CREATE TABLE `auth_group` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `name` varchar(150) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '权限组名称，唯一',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_auth_group_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Django 权限组（框架表，业务侧一般不用）';

-- auth_group_permissions
CREATE TABLE `auth_group_permissions` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `group_id` int NOT NULL COMMENT '权限组 ID',
  `permission_id` int NOT NULL COMMENT '权限项 ID',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_auth_group_permissions_group_permission` (`group_id`,`permission_id`),
  KEY `idx_auth_group_permissions_permission` (`permission_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='权限组与权限的多对多关联（框架表）';

-- auth_permission
CREATE TABLE `auth_permission` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `name` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '权限名称',
  `content_type_id` int NOT NULL COMMENT '所属 ContentType ID',
  `codename` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '权限代号',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_auth_permission_content_type_codename` (`content_type_id`,`codename`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Django 权限项（框架表）';

-- ======== Django ContentType  [django.contrib.contenttypes] ========

-- django_content_type
CREATE TABLE `django_content_type` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `app_label` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Django app 标签',
  `model` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '模型名（小写）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_django_content_type_app_label_model` (`app_label`,`model`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Django 模型类型注册表（框架表）';

-- ======== Django Admin 日志  [django.contrib.admin] ========

-- django_admin_log
CREATE TABLE `django_admin_log` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `action_time` datetime(6) NOT NULL COMMENT '操作时间',
  `object_id` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci COMMENT '被操作对象 ID（字符串）',
  `object_repr` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '被操作对象的展示名',
  `action_flag` smallint unsigned NOT NULL COMMENT '操作类型：1 新增 / 2 修改 / 3 删除',
  `change_message` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '变更摘要',
  `content_type_id` int DEFAULT NULL COMMENT '对象类型 ID',
  `user_id` bigint NOT NULL COMMENT '操作者用户 ID',
  PRIMARY KEY (`id`),
  KEY `idx_django_admin_log_content_type` (`content_type_id`),
  KEY `idx_django_admin_log_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Django Admin 操作日志（框架表）';

-- ======== Django Session  [django.contrib.sessions] ========

-- django_session
CREATE TABLE `django_session` (
  `session_key` varchar(40) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '会话主键（哈希串）',
  `session_data` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '序列化的会话数据',
  `expire_date` datetime(6) NOT NULL COMMENT '会话过期时间',
  PRIMARY KEY (`session_key`),
  KEY `idx_django_session_expire_date` (`expire_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Django 服务端会话（框架表）';

-- ======== Django 迁移记录  [django] ========

-- django_migrations
CREATE TABLE `django_migrations` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '内部自增主键，不对外暴露',
  `app` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Django app 名',
  `name` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '迁移文件名',
  `applied` datetime(6) NOT NULL COMMENT '应用时间',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Django 迁移执行记录；初始化脚本会写入基线，勿手改';

-- ======== Django 迁移基线 ========
-- 不写这段，首次 `manage.py migrate` 会尝试重建所有表并报 "table already exists"。
INSERT INTO `django_migrations` (`id`, `app`, `name`, `applied`) VALUES (1,'contenttypes','0001_initial','2026-11-05 08:49:37.301416'),(2,'contenttypes','0002_remove_content_type_name','2026-11-05 08:49:37.340258'),(3,'auth','0001_initial','2026-11-05 08:49:37.533969'),(4,'auth','0002_alter_permission_name_max_length','2026-11-05 08:49:37.580599'),(5,'auth','0003_alter_user_email_max_length','2026-11-05 08:49:37.586684'),(6,'auth','0004_alter_user_username_opts','2026-11-05 08:49:37.592400'),(7,'auth','0005_alter_user_last_login_null','2026-11-05 08:49:37.599986'),(8,'auth','0006_require_contenttypes_0002','2026-11-05 08:49:37.600864'),(9,'auth','0007_alter_validators_add_error_messages','2026-11-05 08:49:37.604044'),(10,'auth','0008_alter_user_username_max_length','2026-11-05 08:49:37.608598'),(11,'auth','0009_alter_user_last_name_max_length','2026-11-05 08:49:37.614427'),(12,'auth','0010_alter_group_name_max_length','2026-11-05 08:49:37.623582'),(13,'auth','0011_update_proxy_permissions','2026-11-05 08:49:37.631054'),(14,'auth','0012_alter_user_first_name_max_length','2026-11-05 08:49:37.633521'),(15,'accounts','0001_initial','2026-11-05 08:49:37.840786'),(16,'accounts','0002_smscode','2026-11-05 08:49:37.868000'),(17,'admin','0001_initial','2026-11-05 08:49:37.954779'),(18,'admin','0002_logentry_remove_auto_add','2026-11-05 08:49:37.958911'),(19,'admin','0003_logentry_add_action_flag_choices','2026-11-05 08:49:37.964367'),(20,'billing','0001_initial','2026-11-05 08:49:38.090403'),(21,'sessions','0001_initial','2026-11-05 08:49:38.105817'),(22,'tasks','0001_initial','2026-11-05 08:49:38.251846'),(23,'accounts','0003_alter_authtoken_table_comment_and_more','2026-11-05 09:08:18.611815'),(24,'billing','0002_alter_pointledger_table_comment_and_more','2026-11-05 09:08:19.017065'),(25,'tasks','0002_alter_task_status','2026-11-05 09:08:19.022818'),(26,'tasks','0003_alter_task_table_comment_and_more','2026-11-05 09:08:19.738474'),(27,'accounts','0004_captcha_challenge','2026-11-09 07:44:12.660024'),(28,'billing','0003_paymentorder','2026-11-11 08:32:00.000000'),(29,'billing','0004_alter_pointledger_reason_pointlot_pointspend_and_more','2026-11-12 00:00:00.000000');

-- ======== 自检 ========
-- SELECT TABLE_NAME FROM information_schema.TABLES
--  WHERE TABLE_SCHEMA = 'wuhenjiang' ORDER BY TABLE_NAME;   -- 应为 67 行
-- SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
--  WHERE TABLE_SCHEMA = 'wuhenjiang' AND CONSTRAINT_TYPE = 'FOREIGN KEY';  -- 应为 0
