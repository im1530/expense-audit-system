-- ============================================================
-- 初始化数据：expense_audit（与当前开发库保持一致）
-- 生成时间：2026-11-04
-- 用法：先执行 schema.sql，再执行本文件
-- 说明：
--   1) 先插配置类数据（部门/用户/类别/审批流/规则），再插演示用的单据数据；
--   2) 用户密码均为 bcrypt 哈希，无法还原明文；
--   3) 管理员账号：admin / Admin@123456（首次登录后建议立刻改密）。
-- ============================================================

USE `expense_audit`;

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ------------------------------------------------------------
-- 审批流（8 套：默认流 + 单级/多级组合）
-- ------------------------------------------------------------
INSERT INTO `approval_flows` (`id`,`name`,`description`,`levels`,`is_active`,`is_default`,`created_at`,`updated_at`) VALUES
(1,'默认三级审批流','普通报销单:经理→财务→老板 逐级审批','[{"role": "manager", "level": 1}, {"role": "finance", "level": 2}, {"role": "boss", "level": 3}]',1,1,'2026-10-27 00:38:24',NULL),
(2,'经理单人','部门经理一级审批','[{"role": "manager", "level": 1}]',1,0,'2026-10-27 14:26:34',NULL),
(3,'经理→财务','部门经理+财务两级审批','[{"role": "manager", "level": 1}, {"role": "finance", "level": 2}]',1,0,'2026-10-27 14:26:41',NULL),
(4,'经理→财务→老板','三级审批','[{"role": "manager", "level": 1}, {"role": "finance", "level": 2}, {"role": "boss", "level": 3}]',1,0,'2026-10-27 14:26:55',NULL),
(5,'财务单人','财务一级审批','[{"role": "finance", "level": 1}]',1,0,'2026-10-27 14:27:04',NULL),
(6,'财务→老板','财务+老板两级审批','[{"role": "finance", "level": 1}, {"role": "boss", "level": 2}]',1,0,'2026-10-27 14:27:37',NULL),
(7,'其他财务单人','其他财务一级审批','[{"role": "finance_other", "level": 1}]',1,0,'2026-10-27 14:27:44',NULL),
(8,'其他财务→老板','其他财务+老板两级审批','[{"role": "finance_other", "level": 1}, {"role": "boss", "level": 2}]',1,0,'2026-10-27 14:27:53',NULL);

-- ------------------------------------------------------------
-- 审批记录（演示数据：一条通过、一条驳回）
-- ------------------------------------------------------------
INSERT INTO `approvals` (`id`,`expense_id`,`approver_id`,`flow_id`,`status`,`level`,`comment`,`created_at`,`updated_at`) VALUES
(22,20,2,2,'approved',1,'票据合规，经理审核通过','2026-10-28 18:19:28',NULL),
(23,21,2,3,'rejected',1,'票据不齐全，请补充后重新提交','2026-10-29 15:28:48',NULL);

-- ------------------------------------------------------------
-- 附件表：当前库里没有数据，保留建表语句由 schema.sql 负责
-- ------------------------------------------------------------

-- ------------------------------------------------------------
-- 费用类别（15 个小类，覆盖 5 个费用大类）
-- ------------------------------------------------------------
INSERT INTO `categories` (`id`,`name`,`code`,`max_amount`,`description`,`expense_type`,`is_active`,`created_at`,`updated_at`) VALUES
(1,'高铁/动车票','TRAVEL_RAILWAY',2000.00,'差旅途中的高铁、动车火车票费用','transportation',1,'2026-10-28 09:49:05',NULL),
(2,'民航机票','TRAVEL_FLIGHT',5000.00,'差旅途中的飞机票费用','transportation',1,'2026-10-28 09:49:05',NULL),
(3,'长途汽车票','TRAVEL_BUS',500.00,'差旅途中的长途客运汽车费用','transportation',1,'2026-10-28 09:49:05',NULL),
(4,'员工工作餐','MEAL_STAFF',200.00,'员工日常工作餐报销','meal',1,'2026-10-28 09:49:05',NULL),
(5,'业务招待餐','MEAL_ENTERTAIN',2000.00,'客户接待、商务招待餐饮费用','meal',1,'2026-10-28 09:49:05',NULL),
(6,'加班餐费','MEAL_OVERTIME',150.00,'员工加班产生的餐费','meal',1,'2026-10-28 09:49:05',NULL),
(7,'市内打车费','TRANS_TAXI',300.00,'市内出租车、网约车费用','transportation',1,'2026-10-28 09:49:05',NULL),
(8,'公交地铁费','TRANS_PUBLIC',100.00,'市内公交、地铁通勤费用','transportation',1,'2026-10-28 09:49:05','2026-11-03 01:39:59'),
(9,'企业用车','TRANS_COMPANY',800.00,'公司公务用车产生的费用','transportation',1,'2026-10-28 09:49:05',NULL),
(10,'商务酒店','HOTEL_BUSINESS',3000.00,'差旅入住商务酒店费用','accommodation',1,'2026-10-28 09:49:05',NULL),
(11,'快捷酒店','HOTEL_BUDGET',1000.00,'差旅入住经济型快捷酒店费用','accommodation',1,'2026-10-28 09:49:05',NULL),
(12,'办公耗材','OFFICE_SUPPLY',500.00,'纸张、笔、文件夹等日常办公耗材','office',1,'2026-10-28 09:49:05',NULL),
(13,'打印复印','OFFICE_PRINT',200.00,'打印、复印、装订费用','office',1,'2026-10-28 09:49:05',NULL),
(14,'文具用品','OFFICE_STATIONERY',300.00,'各类文具、办公小用品采购','office',1,'2026-10-28 09:49:05',NULL),
(15,'其他杂费','OTHER_MISC',500.00,'无法归类到上述类别的零星费用','other',1,'2026-10-28 09:49:05',NULL);

-- ------------------------------------------------------------
-- 部门（4 个）：研发部/产品部有负责人，应用开发部与财务部暂无负责人
-- ------------------------------------------------------------
INSERT INTO `departments` (`id`,`name`,`description`,`manager_id`,`is_active`,`created_at`,`updated_at`) VALUES
(1,'研发部','技术研发部门',2,1,'2026-10-26 22:20:18','2026-11-03 23:44:34'),
(2,'产品部','产品设计部门',5,1,'2026-10-27 00:03:48','2026-10-27 00:05:03'),
(3,'应用开发部','应用系统开发与维护',NULL,1,'2026-11-03 23:26:40','2026-11-03 23:44:34'),
(4,'财务部','财务核算与报销审核',NULL,1,'2026-11-03 23:26:40','2026-11-03 23:26:40');

-- ------------------------------------------------------------
-- 用户（8 个）：admin / boss / manager×2 / finance×2 / employee×2
-- ------------------------------------------------------------
INSERT INTO `users` (`id`,`username`,`email`,`password_hash`,`full_name`,`phone`,`position`,`role`,`department_id`,`is_active`,`is_superuser`,`must_change_password`,`token_version`,`last_login_at`,`created_at`,`updated_at`) VALUES
(1,'testuser','test@example.com','$2b$12$/m2dclQv3uldRDXNBbcpMetkxrpf.wvzmx.HKRv/5PHLqsO84P1Pi','张三','13800000001',NULL,'employee',1,1,0,0,0,NULL,'2026-10-22 20:35:03','2026-11-03 23:44:29'),
(2,'manager1','manager@example.com','$2b$12$nzHXR75yZWzvY4fHOlJDIujzAQsKqqf/msYp.JId1nQij8KNiBfWe','张经理','13800000002',NULL,'manager',1,1,0,0,0,NULL,'2026-10-23 21:20:10','2026-11-03 23:44:29'),
(3,'fin1','fin1@test.com','$2b$12$o/9b4ffS6Gwp4ivJJ4d6V.qdOjboRUq9DP/n/hfa9r.KkLx44V3D2','王晓丽','13800000003','财务专员','finance',4,1,0,0,0,NULL,'2026-10-26 16:46:29',NULL),
(5,'manager2','manager2@test.com','$2b$12$CZbjlkl7cUjAYIIBct6g..zkrNJNl1KvU9j.opyEC0ssIVSXeuodC','李明远','13800000005','产品部经理','manager',2,1,0,0,0,NULL,'2026-10-27 00:04:08',NULL),
(6,'emp_p2','emp_p2@test.com','$2b$12$5fplo70.xYdResHeK/rmOuIttQoof3u8noF4CXcqd3axkgSjCI1vu','陈思雨','13800000006','产品专员','employee',2,1,0,0,0,NULL,'2026-10-27 00:05:16',NULL),
(7,'boss1','boss1@test.com','$2b$12$DwQ5IMaqBWs4DFUaErH0l.MHNeI9RPVH8UgHt9Nt1if4I1vZ6q0Cy','刘振华','13800000007','总经理','boss',NULL,1,0,0,0,NULL,'2026-10-27 01:06:30',NULL),
(8,'fin2','fin2@test.com','$2b$12$ijUYcIJbgMvBYcegY62xn.uAmEkm0DMxXZi3LmFJema7xNQ9RCO1m','赵雅琴','13800000008','财务主管','finance',4,1,0,0,0,NULL,'2026-10-27 01:56:05',NULL),
(15,'admin','admin@example.com','$2b$12$C2Zg74Gs9SB5EMo9GvY07OOFJ1rrjpasvFP/ocHCpcFJQlop/V3mK','系统管理员','13800000000','系统管理员','admin',3,1,0,0,0,NULL,'2026-11-03 23:28:28',NULL);

-- ------------------------------------------------------------
-- 审批规则（7 条金额类 + 5 条检查类：四类提交检查 + 凭证强度提醒）
-- ------------------------------------------------------------
INSERT INTO `rules` (`id`,`name`,`rule_type`,`condition`,`action`,`config`,`is_active`,`created_at`,`updated_at`) VALUES
(1,'员工小额-经理','amount','employee/admin 且金额<1000,部门经理审批','assign_chain','{"flow_id": 2, "max_amount": 999.99, "min_amount": 0, "submitter_roles": ["employee", "admin"]}',1,'2026-10-27 02:04:30',NULL),
(2,'员工中额-经理→财务','amount','employee/admin 且1000≤金额<20000,经理→财务','assign_chain','{"flow_id": 3, "max_amount": 19999.99, "min_amount": 1000, "submitter_roles": ["employee", "admin"]}',1,'2026-10-27 02:04:30',NULL),
(3,'员工大额-经理→财务→老板','amount','employee/admin 且金额≥20000,经理→财务→boss','assign_chain','{"flow_id": 4, "max_amount": null, "min_amount": 20000, "submitter_roles": ["employee", "admin"]}',1,'2026-10-27 02:04:30',NULL),
(4,'经理小额-财务','amount','manager 且金额<30000,财务审批','assign_chain','{"flow_id": 5, "max_amount": 29999.99, "min_amount": 0, "submitter_roles": ["manager"]}',1,'2026-10-27 02:04:30',NULL),
(5,'经理大额-财务→老板','amount','manager 且金额≥30000,财务→boss','assign_chain','{"flow_id": 6, "max_amount": null, "min_amount": 30000, "submitter_roles": ["manager"]}',1,'2026-10-27 02:04:30',NULL),
(6,'财务小额-其他财务','amount','finance 且金额<20000,其他财务审批','assign_chain','{"flow_id": 7, "max_amount": 19999.99, "min_amount": 0, "submitter_roles": ["finance"]}',1,'2026-10-27 02:04:30',NULL),
(7,'财务大额-其他财务→老板','amount','finance 且金额≥20000,其他财务→boss','assign_chain','{"flow_id": 8, "max_amount": null, "min_amount": 20000, "submitter_roles": ["finance"]}',1,'2026-10-27 02:04:30',NULL),
(8,'类别单次限额提醒','check_category_limit','单条明细金额超过该费用小类的单次报销限额','warn','{}',1,'2026-11-04 15:20:00',NULL),
(9,'发票缺失提醒','check_invoice_missing','明细未上传发票且未填写无票说明','warn','{}',1,'2026-11-04 15:20:00',NULL),
(10,'重复报销提醒','check_duplicate','同一提交人、同一费用日期、同一类别、同一金额在 60 天内重复出现','warn','{"window_days": 60}',1,'2026-11-04 15:20:00',NULL),
(11,'跨期报销提醒','check_period_late','费用发生日期距提交日期超过 60 天','warn','{"max_days": 60}',1,'2026-11-04 15:20:00',NULL),
(12,'弱凭证金额提醒','check_evidence_strength','支付截图等弱凭证金额超过免复核阈值时提示人工复核','review','{"weak_max_amount": 200, "medium_max_amount": 1000, "category_overrides": {"transportation": 300}}',1,'2026-11-07 02:00:00',NULL);

-- ------------------------------------------------------------
-- 演示单据（5 张）：覆盖 approved / pending / draft 三种状态
-- ------------------------------------------------------------
INSERT INTO `expenses` (`id`,`user_id`,`expense_no`,`title`,`total_amount`,`status`,`flow_id`,`approval_chain`,`current_level`,`description`,`remark`,`submitted_at`,`approved_at`,`paid_at`,`cancelled_at`,`risk_score`,`risk_level`,`ai_reviewed_result`,`created_at`,`updated_at`) VALUES
(20,1,'EXP2026102817494837D05F','审批轨迹测试-深圳出差报销',500.00,'approved',2,NULL,1,'测试审批轨迹专用单据',NULL,'2026-10-28 17:55:40','2026-10-28 18:19:29',NULL,NULL,NULL,NULL,NULL,'2026-10-28 17:49:48','2026-10-28 18:19:28'),
(21,1,'EXP2026102915273515EB9B','9月广州出差差旅费报销',2140.50,'pending',3,NULL,1,'赴广州参加行业会议产生的差旅费用','附电子发票及行程单','2026-10-29 15:29:18','2026-10-29 15:28:49',NULL,NULL,NULL,NULL,NULL,'2026-10-29 15:27:35','2026-10-29 15:29:18'),
(22,1,'EXP202610292038477A6432','明细功能测试单',100.00,'draft',NULL,NULL,1,'用于测试明细增删改功能','测试专用',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-10-29 20:38:47',NULL),
(23,1,'EXP20261029204506F531DC','明细功能测试单',600.00,'draft',NULL,NULL,1,'用于测试明细增删改功能','测试专用',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-10-29 20:45:06','2026-10-29 21:06:19'),
(24,7,'EXP202611021735025CEB95','10月差旅费用报销',1225.50,'draft',NULL,NULL,1,'广州出差交通、住宿费用报销','附纸质发票已提交财务',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'2026-11-02 17:35:02',NULL);

-- ------------------------------------------------------------
-- 演示明细（9 条，挂在上面 5 张单据下）
-- ------------------------------------------------------------
INSERT INTO `expense_items` (`id`,`expense_id`,`category_id`,`description`,`amount`,`expense_date`,`invoice_no`,`invoice_url`,`created_at`,`updated_at`) VALUES
(21,20,1,'广州南-深圳北高铁二等座',300.00,'2026-10-27',NULL,NULL,'2026-10-28 17:49:48',NULL),
(22,20,7,'深圳北站到酒店打车费',200.00,'2026-10-27',NULL,NULL,'2026-10-28 17:49:48',NULL),
(23,21,1,'深圳北-广州南往返高铁二等座',860.50,'2026-09-15',NULL,NULL,'2026-10-29 15:27:35',NULL),
(24,21,10,'广州天河区商务酒店住宿2晚',1280.00,'2026-09-15',NULL,NULL,'2026-10-29 15:27:35',NULL),
(25,22,1,'初始明细-高铁票',100.00,'2026-10-29',NULL,NULL,'2026-10-29 20:38:47',NULL),
(26,23,1,'初始明细-高铁票',100.00,'2026-10-29',NULL,NULL,'2026-10-29 20:45:06',NULL),
(29,23,10,'新增测试-宾馆',500.00,'2026-10-29',NULL,NULL,'2026-10-29 21:05:19',NULL),
(30,24,3,'高铁往返车票',326.50,'2026-10-25','FP20261025001','https://xxx.com/invoice/001.pdf','2026-11-02 17:35:02',NULL),
(31,24,5,'酒店住宿2晚',899.00,'2026-10-26','FP20261026007','https://xxx.com/invoice/007.pdf','2026-11-02 17:35:02',NULL);

SET FOREIGN_KEY_CHECKS = 1;
