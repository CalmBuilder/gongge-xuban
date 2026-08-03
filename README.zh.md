# 共格·序伴

<p align="center">
  <img src="packaging/assets/gongge-xuban-mark.svg" width="112" alt="共格·序伴标识" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/Node.js-20%2B-339933" alt="Node.js 20+" />
  <img src="https://img.shields.io/badge/FastAPI-0.139%2B-009688" alt="FastAPI" />
  <img src="https://img.shields.io/badge/React%2018-TypeScript%20strict-3178C6" alt="React 18 + TypeScript strict" />
  <img src="https://img.shields.io/badge/DB-SQLite%20%C2%B7%20MySQL%208.4-4479A1" alt="SQLite / MySQL 8.4" />
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh.md">中文</a>
</p>

共格·序伴是一套面向企业的数字员工构建、运行与治理平台。它帮助专业人员将知识、流程、工具和判断标准沉淀为可复用的组织能力——每一次执行都有记录、可审计、可复现。

## 平台亮点

- **统一 Agent Loop 阶段协议**——所有会话由同一份跨模块契约驱动：路由 → SOP → 工具 → 知识 → 通用技能 → 反思 → 回复；经 SSE 流式下发，支持取消与失败修复路径，并由后端生产者与前端消费者的端到端回归测试共同覆盖。
- **统一 SOP 元模型**——状态机执行、版本化定义、幂等执行存储、条件 DSL、显式确认与工作项，配套批量迁移工具；20 个内置 SOP 全部按同一元模型编译。
- **可审计的工具与技能执行**——HTTP 工具、MCP 客户端与内置服务、生成代码通用技能 runner（Python/Bash），以超时、包白名单、权限校验、路径与 shell 防护作为执行边界。
- **可治理的知识底座**——受管知识库支持 PDF/DOCX/HTML 解析、引用追踪与访问治理，回答可回溯到来源。
- **企业治理闭环**——API 层租户隔离与角色校验、高风险操作人工审批、管理审计、执行追踪与事件日志、定时任务与长期记忆。
- **双方言持久化**——SQLite 支撑零配置开发与桌面形态，MySQL 8.4（`utf8mb4`、UTC）支撑服务端部署；schema 由 Alembic 统一版本化，方言差异收敛在适配层边界内。
- **一套代码，三种运行形态**——浏览器应用、单端口服务、PyInstaller 打包的桌面客户端（macOS / Windows / Linux），配套签名与公证流水线。

## 架构总览

```text
React 18 工作台（对话 · 管理 · 审计）                    frontend-enterprise/
        │  HTTP + SSE
FastAPI API（认证 · 租户隔离 · 角色校验）                 backend/app/api/
        │
Agent Loop：路由 → SOP → 工具 → 知识                     backend/app/llm/ + app/sop_runtime/
            → 通用技能 → 反思 → 回复
        │
┌───────────────┬────────────────┬──────────────┬───────────────────┐
SOP 运行时       工具执行          知识库          调度 · 审批
状态机 · 版本 ·   HTTP · MCP ·    解析 · 引用 ·    · 审计 · 追踪
幂等             生成代码（沙箱）   治理            · 记忆
        │
SQLite（开发/桌面）  ·  MySQL 8.4（服务端，Alembic 迁移）
```

## 环境要求

- Python 3.11 或更高版本
- Node.js 20 或更高版本
- npm
- 可选：用于服务端部署的 Docker Compose 与 MySQL 8.4

## 快速启动

### 1. 安装依赖

macOS、Linux 或 WSL：

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

cd ../frontend-enterprise
npm ci
cd ..
```

Windows PowerShell：

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env

cd ..\frontend-enterprise
npm ci
cd ..
```

### 2. 启动与管理

| 操作 | macOS、Linux 或 WSL | Windows PowerShell |
| --- | --- | --- |
| 开发模式 | `./app.sh dev` | `.\app.ps1 dev` |
| 后台生产模式 | `./app.sh` | `.\app.ps1` |
| 查看状态 | `./app.sh status` | `.\app.ps1 status` |
| 停止服务 | `./app.sh stop` | `.\app.ps1 stop` |

访问 `http://localhost:5137`，Swagger UI 位于 `http://localhost:5137/docs`。
开发环境默认管理员账号为 `admin` / `admin`，首次登录后请立即修改密码。

两套包装脚本都调用统一的 `scripts/app.py` 生命周期入口。也可以绕过包装脚本，直接使用项目虚拟环境：

| 平台 | 后台生产模式示例 |
| --- | --- |
| macOS、Linux 或 WSL | `backend/.venv/bin/python scripts/app.py up --detach` |
| Windows PowerShell | `.\backend\.venv\Scripts\python.exe scripts\app.py up --detach` |

需要执行其他操作时，将 `up --detach` 替换为 `up --mode development`、`status` 或 `down`。

## 数据库

桌面版与零配置开发默认使用 SQLite。需要使用仓库内的 MySQL 服务时，先配置根目录 `.env`，再执行：

```bash
docker compose up -d mysql
cd backend
.venv/bin/python -m alembic -c alembic.ini upgrade head
```

在 `backend/.env` 中配置应用连接：

```dotenv
DATABASE_URL="mysql+pymysql://gongge_xuban:URL_ENCODED_PASSWORD@127.0.0.1:3306/gongge_xuban?charset=utf8mb4"
```

配置优先级为：操作系统环境变量、`backend/.env`、`backend/app/config.py` 默认值。MySQL 部署始终通过 Alembic 升级 schema（`alembic -c alembic.ini upgrade head`），任何建表快捷方式都不能替代迁移。

## 开发检查

```bash
cd frontend-enterprise
npm run brand:check:test
npm run brand:check
npm run i18n:check
npm run test:run
npm run test:e2e
npm run test:e2e:fullstack
npm run build

cd ../backend
.venv/bin/ruff check .
.venv/bin/pytest
```

质量门禁包括 135+ 后端 pytest 测试模块、40+ 前端 Vitest 测试文件（Testing Library 行为断言）、品牌字面量与 i18n 键扫描器、Ruff（Python 3.11，行宽 100）以及 strict 模式的 TypeScript 构建。

真实浏览器回归使用 Playwright Chromium。首次运行前在
`frontend-enterprise/` 执行 `npm run test:e2e:install`；精简 Linux 环境还需按
Playwright 提示安装 Chromium 系统依赖。`test:e2e:fullstack` 会构建前端、启动真实
FastAPI，并使用进程退出后自动清理的临时 SQLite 数据库，不连接开发数据库。

## 桌面打包

各平台打包入口位于 `packaging/`：`build_macos.sh`、`build_windows.ps1`、`build_linux.sh`。生成的桌面资产统一使用 `gongge-xuban` 标识，应用 ID 为 `cn.gongge.xuban.desktop`。

在 macOS 原生构建机上运行 `bash packaging/build_macos.sh`：脚本会先按 `backend/pyproject.toml` 对齐打包环境，签名 `.app`，在构建机原生架构上启动该打包产物并校验产品专属健康响应，通过后才生成 DMG。Developer ID 签名与公证由 `MAC_SIGN_ID` 和 `NOTARY_PROFILE` 控制。Windows 签名由 `packaging/sign_windows.ps1` 处理，详见 [packaging/WINDOWS_SIGNING.md](packaging/WINDOWS_SIGNING.md)。

## 目录结构

```text
backend/                 FastAPI 应用、任务执行、数据持久化与测试
frontend-enterprise/     React 与 TypeScript 工作台
packaging/               桌面构建配置与品牌资产
scripts/                 本地进程管理
```

产品手册与参考源码维护在 git 树之外，不随仓库分发。

## 文档导航

- [后端说明](backend/README.md)——后端运行与开发入口
- [前端说明](frontend-enterprise/README.md)——前端工作区入口
- [协作约定（AGENTS.md）](AGENTS.md)——架构规则、质量门禁、交付清单
- [Windows 签名指南](packaging/WINDOWS_SIGNING.md)——证书与签名流水线

## 安全与使用边界

模型输出可能不完整或不准确，外部工具和生成代码也可能产生真实副作用。请使用最小权限凭据，并为高风险操作配置人工审批。本平台不能替代法律、医疗、金融、安全等受监管领域的专业审核。
