-- gpu_worker MySQL 8 初始化脚本（对应 core/migrations/0001_initial.py）。
-- 默认库名与 .env.example 的 WORKER_MYSQL_NAME 一致；使用其他库名时先替换下方两处。
-- 在空库上执行一次。执行后可运行 manage.py migrate 补齐 Django 自带应用的迁移。

-- 数据库用途：算力节点任务镜像与阶段日志。
CREATE DATABASE IF NOT EXISTS `worker_logs`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `worker_logs`;

CREATE TABLE `local_task` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '本地任务记录主键',
  `task_id` varchar(64) NOT NULL COMMENT '云端任务 ID',
  `status` varchar(20) NOT NULL COMMENT '本地状态：运行中、成功、失败或中止',
  `duration_seconds` double NOT NULL COMMENT '视频时长，单位秒',
  `stage` varchar(32) NOT NULL COMMENT '当前或最后处理阶段',
  `progress` double NOT NULL COMMENT '处理进度，范围 0 到 1',
  `attempts` int NOT NULL COMMENT '本地处理尝试次数',
  `metrics` json NOT NULL COMMENT '验收指标 JSON',
  `error_code` varchar(64) NOT NULL COMMENT '失败错误码，无错误时为空',
  `error_message` longtext NOT NULL COMMENT '失败原因详情',
  `started_at` datetime(6) NOT NULL COMMENT '本地开始处理时间',
  `finished_at` datetime(6) NULL COMMENT '本地处理结束时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `task_id` (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='算力节点本地任务镜像；权威状态在云端';

CREATE TABLE `stage_log` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '阶段日志主键',
  `task_id` varchar(64) NOT NULL COMMENT '云端任务 ID',
  `stage` varchar(32) NOT NULL COMMENT '处理阶段名称',
  `exit_code` int NOT NULL COMMENT '阶段进程退出码',
  `elapsed_seconds` double NOT NULL COMMENT '阶段耗时，单位秒',
  `tail` longtext NOT NULL COMMENT '阶段输出末尾日志',
  `created_at` datetime(6) NOT NULL COMMENT '日志创建时间',
  PRIMARY KEY (`id`),
  KEY `stage_log_task_id_idx` (`task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='各处理阶段的退出码、耗时与输出日志';

-- django.contrib.contenttypes 当前迁移完成后的结构。
CREATE TABLE `worker_django_content_type` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '内容类型主键',
  `app_label` varchar(100) NOT NULL COMMENT 'Django 应用标签',
  `model` varchar(100) NOT NULL COMMENT 'Django 模型名称',
  PRIMARY KEY (`id`),
  UNIQUE KEY `worker_content_type_app_model_uniq` (`app_label`, `model`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Django 内容类型登记表';

-- 记录 core 初始迁移已由本脚本创建，避免后续 migrate 重复建表。
CREATE TABLE `worker_django_migrations` (
  `id` bigint NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '迁移记录主键',
  `app` varchar(255) NOT NULL COMMENT 'Django 应用标签',
  `name` varchar(255) NOT NULL COMMENT '迁移文件名称',
  `applied` datetime(6) NOT NULL COMMENT '迁移应用时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Django 数据库迁移记录';

INSERT INTO `worker_django_migrations` (`app`, `name`, `applied`) VALUES
  ('contenttypes', '0001_initial', UTC_TIMESTAMP(6)),
  ('contenttypes', '0002_remove_content_type_name', UTC_TIMESTAMP(6)),
  ('core', '0001_initial', UTC_TIMESTAMP(6));
