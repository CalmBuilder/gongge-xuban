# Backend

FastAPI backend for 共格·序伴.

反馈材料的本人预览、明确分享、撤回与最小投影契约见
[反馈模块说明](app/feedback/README.md)。
反馈候选接入当前编辑器、明确重载、人工发布和精确版本身份的合同见
[SOP 编辑模块说明](app/skills/README.md)。

## Run

From the repository root, prefer:

```bash
./app.sh dev
```

For backend-only debugging:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
.venv/bin/uvicorn single_port_app:app --host 0.0.0.0 --port 5137
```

Health endpoint: `http://localhost:5137/api/health`

`CORS_ORIGINS` controls the allowed frontend origins. Application settings use
operating-system environment variables first, then `backend/.env`, then defaults
from `backend/app/config.py`.

## Database

SQLite remains the default for desktop and zero-configuration development. The
backend also supports MySQL 8.4 through SQLAlchemy and PyMySQL. Database selection
is controlled only by `DATABASE_URL`; application code uses the same SQLAlchemy
models and session API for both databases.

To use the repository MySQL service:

```bash
docker compose up -d mysql
./db.sh
```

Set a URL-encoded application-account URL in `backend/.env` before running the
migration or starting 共格·序伴:

```dotenv
DATABASE_URL="mysql+pymysql://gongge_xuban:URL_ENCODED_PASSWORD@127.0.0.1:3306/gongge_xuban?charset=utf8mb4"
DATABASE_POOL_SIZE="10"
DATABASE_MAX_OVERFLOW="20"
DATABASE_POOL_TIMEOUT_SECONDS="30"
DATABASE_POOL_RECYCLE_SECONDS="1800"
```

Do not use the MySQL root account as the application account. Characters such as
`&`, `%`, `@`, `/`, and `:` in a password must be percent-encoded inside a URL.
The MySQL adapter checks the Alembic revision at startup and refuses to run against
an empty or stale schema. The unified `app.sh` launcher performs a bounded (10
second) read-only check before starting the supervisor; when migration is needed it
exits promptly and prints the current/head revisions plus the command
`./db.sh`. Run `./db.sh` explicitly after pulling a new migration; it upgrades to
Alembic `head` and verifies the result. Use `./db.sh check` for a read-only check.
SQLite keeps its legacy in-process upgrade path for compatibility and is skipped by
this MySQL-only script.

The root `.env` configures only the Compose container (`MYSQL_DATABASE`,
`MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_ROOT_PASSWORD`). `backend/.env` configures
the application. Keep both `.env` files
out of version control. Stop the container without deleting its bind-mounted data:

```bash
docker compose stop mysql
```

MySQL 标记测试会创建随机测试数据库和运行账号，测试结束后自动清理。若没有显式设置
`MYSQL_TEST_ADMIN_URL`，`backend/tests/conftest.py` 会在进程内从根目录 `.env` 的
`MYSQL_ROOT_PASSWORD`、`MYSQL_BIND_ADDRESS` 和 `MYSQL_PORT` 构造管理员 URL，不会把密码复制到
源码或测试文件；需要指定客户端地址时可设置 `MYSQL_TEST_ADMIN_HOST`。若 MySQL 只允许本机
回环连接，应将该变量设为 `127.0.0.1`，或为测试来源配置对应的 MySQL `root@host` 账户。
测试夹具只操作随机临时库，不操作正式租户库。

## Agency Agents 平台内置专家（随应用发布）

正式发布的共格·序伴会把当前已审核的 Agency Agents 中文专家包随应用交付，固定资源位于
`app/experts/data/agency_agents_builtin_v2.json`。当前包对应上游提交
`3c9588880b7cafaec325a104899fd8bbe27e7d72`，包含 273 条专家；应用首次启动和后续启动都会将
它们幂等写入各租户，状态为 `active`、已发布到专家模板目录、审核通过且可用。桌面
EXE 通过 PyInstaller spec 显式携带该文件，因此运行时不需要 GitHub、`agency-agents-library/`
或人工导入，安装后即可看到内置专家。生产启动会为已有且存在在职管理员的每个租户分别补齐
同一份内置专家；新租户在普通用户首次登录时懒补齐，普通用户无需承担模板所有权即可从开放广场
查看并直接使用。

内置 Skill 也在启动初始化中导入固定资源，并自动完成首批审核发布：`GeneralSkill.status` 和
当前 `GeneralSkillRevision.status` 均为 `published`，目录元数据为 `review_status=approved`。
Skill 的发布与“绑定到某个租户 Agent”仍是两个动作；已发布 Skill 可被目录发现和显式安装/绑定，
不会未经授权自动挂到所有员工。普通用户从开放广场点击“安装到能力分身”时会固定当前已发布
修订；若该用户还没有能力分身，系统只在这次明确安装动作中创建一个私有能力分身，再完成绑定。

## Agency Agents 离线升级工具（可选本地输入）

`../agency-agents-library/` 是升级审计和历史导入的本地输入目录，不是运行时目录，也不随 GitHub
源码发布；运行时不会自动连接 GitHub。若需要把未来上游版本纳入下一次产品发布，可使用
`app.experts.sync_cli` 按提交号生成计划、审核、应用和回滚；同步流程只处理本地输入，不执行
远程获取。输入包移除只会进入报告，不会自动删除租户专家；完成审核后需重新生成并交付固定内置
专家包，才能进入下一版 EXE。

每次 apply 结果包含更新前快照、应用后内容/元数据摘要和版本号；需要撤销时使用：

```bash
.venv/bin/python -m app.experts.sync_cli rollback \
  --tenant-id tenant_demo --admin-username admin \
  --result /tmp/agency-agents-sync-apply.json
```

回滚会拒绝已发布、已绑定、已使用或 apply 后被修改的专家。分类是独立的显式步骤，使用
`app.experts.taxonomy_cli check/apply`，MySQL 应先执行 Alembic migration；分类 apply 只更新
四个专家分类元数据字段。

## General Skill Code Runtime

通用技能生成的 Python/Bash runner 不直接依赖系统 Python。运行时按以下顺序选择环境：

1. `GENERAL_SKILL_RUNTIME_PYTHON` 指定的 Python；
2. `GENERAL_SKILL_RUNTIME_VENV` 指定虚拟环境中的 Python；
3. `backend/.venv/bin/python`；
4. 自动创建 `backend/.runtime_venv`。

`GENERAL_SKILL_RUNTIME_PACKAGES` 默认安装/校验 `requests,httpx`，用于通用 API
访问。需要文档解析或数据处理时可以扩展为：

```bash
GENERAL_SKILL_RUNTIME_PACKAGES="requests,httpx,beautifulsoup4,lxml,pypdf,python-docx,pandas,numpy,python-dateutil"
```

如果部署环境禁止自动安装依赖，设置：

```bash
GENERAL_SKILL_RUNTIME_AUTO_INSTALL="false"
GENERAL_SKILL_RUNTIME_PYTHON="/path/to/prepared/venv/bin/python"
```

## Demo Seed

普通 DynamicTaskAgent 的已发布 HTTP 外部写沿原一次性 Attention 批准执行。工具须显式启用
`dynamic_task_enabled`、声明 `external_write / once / unknown / request_key`，且命中现有
external-write 租户和员工灰度及运行阈值配置。发起人与活动管理员批准人分离，批准只绑定
本次冻结参数、能力 checksum、计划和有效期；派发前再次核当前授权、员工生命周期与原
owner/fence。此路径不采用企业微信长期批准规则。

`provider_task` 受理只进入原 Operation/Attempt 的 `waiting_external`；原 Inbox/Signal 和
DTA 消费者根据本次远端任务恢复并核验成果。已发请求中断、超时或效果不明只进入原对账，
不沿本地工具的幂等重试路径重发。刷新或重复批准回执不会新增提交。

`ToolReliabilityContract` 默认使用中性的 `async_strategy=internal_worker` 和同步
`execution_mode=sync`；只有明确声明 `async_strategy=provider_task` 的外部任务才允许
`execution_mode=detached`，并必须同时提供状态查询地址、字段映射和跟踪期限。这样本地
工具不会因缺省字段被误解释为远端任务，显式远端合同仍沿上面的 Operation/Attempt/Inbox
恢复与对账链路执行。历史库中曾保存的旧后台策略值只在读取和快照校验时兼容，新的发布
和写回统一使用 `internal_worker`，不会重新产生旧来源名称。

SOP 外部任务完成后，原恢复事务将确认的业务结果交付到原会话。最终回复绑定原 Operation
节点的致因输入、时间和冻结技能版本，并在当前成员、员工使用权限和执行所有权校验后持久化；
重复回执不新增最终回复，写入失败保留原 Signal 退避恢复，取消或中断轮次不接收迟到成功。
若原等待回复已经封口，同步 Turn 不会重新 complete；结果由持久消息及原会话状态投影供刷新读回。
用户正文、交付事件和会话摘要只采用原 Operation 冻结的 `user_view.output_schema`，
工具当前后继放宽不会扩大旧结果可见字段。缺少冻结输出 schema 时不回退显示原始结果；
`audit_only` 字段和完整供应商回执仍保留在原 Operation、Attempt 与 Inbox 审计账本。
旧实例若只保留会话 ID 来源且无节点致因时间，继续沿原 Runtime 完成；不从现存消息猜造最终聊天交付来源。

SOP 已发送动作的结果未知时，原工具结果入口复用唯一异常 Attention，不重新提交。
办理人在原提交事务中须仍为活动成员且具有对应治理权限；旧候选身份不能覆盖撤权。
原 Attention API 在同一 owner/fence、命令 CAS 和事务内消费唯一对账 Signal，结算原
Operation/Attempt。确认已生效后沿原冻结定义恢复；确认未生效按原失败收敛；已请求取消
只如实记账并关闭，不创建正常后继。对账 comment 仅留私密审计，不能充当业务结果或最终回复。
普通资料入口将员工置为归档，或原发起人的成员/使用资格撤销时，合法治理人仍可结算
原效果；随后沿原失败状态关闭，不伪造用户取消，不交付成功消息。原异步恢复和对账
后继的HTTP派发复用ToolExecutor原发送前回调，重核当前来源、工具绑定/冻结合同及
owner/fence，并由原执行器提交短事务后进入运输，避免网络期间持有数据库事务。

普通流式聊天成功完成时，包括未绑定已发布 SOP 和 Router 只回答的轮次，同轮用户来源、
助手消息与持久记忆提取任务由原 `complete` 事务一并提交；提交后才通知 worker。
若完成提交被撤权或取消屏障拒绝，不留下可派发任务。通知丢失或测试服务重启后，
原持久扫描器仍按来源、助手、版本及权限 generation 核验并结算，管理读取保持完整。

Startup seeds:

- `tenant_demo`
- refund skill `after_sales_refund`
- exchange skill `after_sales_exchange`
- mock HTTP tool `order.query`

Set `DEMO_MODEL_API_KEY` before first startup if you want a default model config to be created automatically.

动态任务已完成知识检索后，后继模型请求会继续携带该步骤证据。每次 SDK 派发及响应返回后复用原成员/员工/精确版本授权检查；知识 head 切换使原版本失权时，禁止再次披露或保存其后继提案，原 Signal 按稳定来源拒绝终结。仍有当前合法旧版本绑定的实例继续按冻结版本恢复；检索回执和已执行远端事实保持原账本。
知识稳定拒绝会提交释放本次模型租约；外部回执已经消费后发生的拒绝仍由原 `fail_execution` 和终态闭合守卫收敛，保留成功 Operation 与已消费 Signal，不重放回执。
