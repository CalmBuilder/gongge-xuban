# Backend

FastAPI backend for 共格·序伴.

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

Swagger UI: `http://localhost:5137/docs`

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
cd backend
.venv/bin/python -m alembic -c alembic.ini upgrade head
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
an empty or stale schema; apply `alembic upgrade head` explicitly after pulling a
new migration. SQLite keeps its legacy in-process upgrade path for compatibility.

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

## Agency Agents 专家同步

专家同步必须使用按上游提交号保存的导入包和同批次中文化包，先生成只读计划，再逐路径批准
`apply`；上游移除只会进入报告，不会自动删除租户专家。SQLite 与 MySQL 使用同一套同步逻辑：

```bash
.venv/bin/python -m app.experts.sync_cli plan \
  --tenant-id tenant_demo --admin-username admin \
  --input ../agency-agents-import-<commit> \
  --baseline-input ../agency-agents-import-<old-commit> \
  --baseline-localization ../agency-agents-zh-<old-commit> \
  --output /tmp/agency-agents-sync-plan.json

.venv/bin/python -m app.experts.sync_cli apply \
  --tenant-id tenant_demo --admin-username admin \
  --input ../agency-agents-import-<commit> \
  --plan /tmp/agency-agents-sync-plan.json \
  --localization ../agency-agents-zh-<commit> \
  --approve-path path/to/expert.md \
  --acknowledge-review path/to/expert.md=high_risk_content
```

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

Startup seeds:

- `tenant_demo`
- refund skill `after_sales_refund`
- exchange skill `after_sales_exchange`
- mock HTTP tool `order.query`

Set `DEMO_MODEL_API_KEY` before first startup if you want a default model config to be created automatically.
