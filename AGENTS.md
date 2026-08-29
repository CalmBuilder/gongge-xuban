# AGENTS.md

本文件适用于仓库根目录及全部子目录。开始修改前先阅读与任务直接相关的代码、测试和文档；若子目录以后增加更具体的 `AGENTS.md`，以更深层文件为准。

## 项目概览

共格·序伴是企业数字员工平台。主要运行链路为：React 管理/聊天端 → FastAPI API → Agent Loop（路由、SOP、工具、知识、通用技能、反思、回复）→ SQLite 或 MySQL。

- `backend/`：Python 3.11+、FastAPI、SQLModel/SQLAlchemy、Alembic、pytest、Ruff。
- `frontend-enterprise/`：React 18、TypeScript 6、Vite 6、Tailwind CSS 4、Vitest、Testing Library。
- `scripts/` 与 `app.sh`：本地统一启动和进程管理。
- `packaging/`：PyInstaller 及 Linux、macOS、Windows 桌面打包。
- `docs/Module_Description/`：后端、前端和 Agent Loop 的详细模块手册；它适合导航，但依赖版本和文件数量可能滞后，最终以源码和清单文件为准。
- `docs/superpowers/specs/`、`docs/superpowers/plans/`：历史设计和实施记录，不等同于当前待办。
- `agency-agents-zh/`、`agency-agents-import/`：专家中文化与一次性导入工作树/产物。
- `otherpro/`：只读上游或参考源码，不参与主项目构建，不要修改，也不要让生产代码或测试依赖它。

不要编辑生成目录或本地状态，例如 `node_modules/`、`dist/`、`build/`、`*.egg-info/`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、`.venv/`、`.runtime_venv/`、`packaging/out/` 和数据库文件。

## 开始工作

1. 定位调用方、数据契约和相邻测试；不要仅根据文件名猜测行为。
2. 检查工作区现有改动并保留它们。当前分发环境可能没有 Git 元数据，不要把 Git 命令作为完成任务的前提。
3. 优先做范围最小、可回归验证的修改。不要顺手重构无关代码、批量格式化或升级依赖。
4. 行为变更应先补充或更新能复现该行为的测试；修复后先跑目标测试，再跑受影响子系统检查。
5. 不把密码、API Key、JWT、真实数据库 URL、用户数据或内部服务凭据写入源码、测试、文档和命令输出。

## SOP 批次双审核门禁

每一批 SOP 元模型或 Runtime 改造在标记完成、进入下一批之前，必须分别完成并记录以下两项审核：

1. **参考来源与源码偏差审核**：说明本批能力分别借鉴了 Flowable、Conductor、Windmill、SpiffArena 的哪些思想、契约、代码逻辑、流程或测试组织；只对实际相关的唯一主参考做具体 revision/路径/行为对比，记录采用、调整、拒绝和偏差，禁止笼统宣称“四个项目都已参考”。若能力是本项目适配性扩展，也必须给出成熟来源的交叉证据、项目约束和行为测试，不能伪装成上游原生能力。
2. **统一元模型方向审核**：独立检查本批是否继续服务业务闭环、确定性、统一元模型、版本/幂等/事件/状态机鲁棒性、当前 20 个及未来 SOP 的适应性，以及现有技术栈和 tenant/双数据库边界。该审核拥有否决权；即使实现与参考项目一致，只要会引入第二套语义、无用复杂度或不适配本项目，就必须调整或拒绝。

审核结论和源码证据写入 `docs/SOP现状与规划/开源参考项目使用记录.md`，批次状态同步到相应开工/规划文档。两项审核都通过后才能宣称本批完成；参考项目用于提高成熟度和查漏，不用于替代本项目目标判断，也不要求每批机械使用全部四个项目。

## 安装与运行

从仓库根目录初始化：

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

cd ../frontend-enterprise
npm ci
```

首选统一开发入口：

```bash
./app.sh dev       # 前台开发模式
./app.sh           # 后台生产式模式
./app.sh status
./app.sh stop
```

统一应用和 Swagger 默认位于 `http://localhost:5137` 与 `/docs`。直接运行 `npm run dev` 时也使用 5137，但它只用于前后端拆分调试；此时要核对 `VITE_API_BASE_URL`/Vite proxy，不能与统一启动链路同时占用该端口。

## 验证命令

后端（从 `backend/` 执行）：

```bash
.venv/bin/ruff check .
.venv/bin/pytest tests/path/to/test_file.py -q
.venv/bin/pytest
```

前端（从 `frontend-enterprise/` 执行）：

```bash
npm run test:run -- src/path/to/file.test.tsx
npm run brand:check:test
npm run brand:check
npm run i18n:check
npm run test:run
npm run build
```

- 文档或注释专用修改至少运行与其覆盖文件相符的静态检查；纯 Markdown 修改可做内容和链接检查，无需伪称跑过应用测试。
- 前端逻辑/UI 修改至少运行目标 Vitest 和 `npm run build`；涉及品牌或文案时再跑品牌/i18n 门禁。
- 后端修改至少运行目标 pytest 和 Ruff；跨模块核心链路、数据库模型或公共契约修改应运行完整后端测试。
- 标记为 `mysql` 的集成测试优先使用 `MYSQL_TEST_ADMIN_URL`，未设置时可由测试夹具在进程内从根 `.env` 的 `MYSQL_ROOT_PASSWORD`、`MYSQL_BIND_ADDRESS` 和 `MYSQL_PORT` 构造；夹具只临时创建并清理随机数据库和用户，不得改写真实租户库。两者都未配置时跳过才是预期行为。
- 只报告实际运行过的命令和结果；若未运行某项，说明原因。

## 后端约定

- 保持分层：`app/api/` 负责 HTTP 输入、认证和响应；领域行为放在相应 service/runtime 模块；`app/db/` 负责模型、会话和方言适配。
- API 必须维持 tenant 隔离和角色校验。新管理端路由通常依赖 `get_current_user`，并验证请求 tenant 与当前用户一致；不要相信客户端传入的 tenant、agent 或资源归属。
- 请求/响应使用明确的 Pydantic 模型和类型注解。沿用附近代码的异常状态码、事务边界以及 `db.add`/`commit`/`refresh` 模式。
- 数据库代码同时考虑 SQLite 开发/桌面模式和 MySQL 8.4。避免只在单一方言可用的 SQL；确需方言差异时放入现有 adapter/兼容边界并覆盖双方言行为。
- 修改 SQLModel 表结构时同步模型、Alembic 迁移和迁移测试。MySQL 启动不会自动修复过期 schema；使用 `alembic -c alembic.ini upgrade head`。不要用 `SQLModel.metadata.create_all()` 代替 MySQL 迁移。
- 时间统一使用项目的 UTC 辅助函数/带时区语义，不新增本地时区假设。
- Agent Loop、SSE、LLM 阶段协议和会话 schema 是跨模块契约。变更事件名、payload、状态字段、prompt 输出结构或取消逻辑时，同时检查后端生产者、前端消费者、持久化和回归测试。
- prompt 位于 `backend/app/llm/prompts/`。修改时保留统一阶段协议、结构化输出约束和失败/修复路径，避免把可变业务规则硬编码到通用 LLM 客户端。
- 通用技能 runner 会执行生成的 Python/Bash；保持现有超时、包白名单、权限、路径和 shell 防护，不放宽执行边界来绕过测试。
- 本项目后续新增或实质修改的 Python 文件必须在文件顶部维护模块 docstring，依次包含 `@Time`、`@Author`、`@File`、`@CallChain`、`@Description`；作者统一写 `zhanglp8181`，时间、文件名、调用链和职责必须与实际内容一致。
- 后续新增或实质修改的函数、实例方法、类方法、静态方法和测试方法必须有描述真实职责、输入边界或关键行为的中文 docstring；不要生成“处理数据”一类空泛注释，也不要为了统一而批量修改未触及文件。
- Ruff 配置为 Python 3.11、行宽 100。遵循现有导入顺序与现代类型语法。

## 前端约定

- TypeScript 保持 `strict`，不要用 `any`、无依据的类型断言或重复定义后端已有契约。共享 API 类型放在 `src/types/` 或相应领域类型文件。
- API 调用集中经 `src/api/`，保持认证头、`ApiError`、same-origin 基址和 SSE 解析行为；不要在页面中散落裸 `fetch`。
- 新页面和重构优先使用 `@/components/ui` 的 shadcn/ui 组件、`@/lib/utils` 的 `cn()`、`sonner` 的 `toast`。不要新增 Ant Design；现有旧组件只在任务明确要求时迁移。
- 复用 `src/lib/enterprise-ui.ts` 的共享样式令牌和现有设计语言，避免在相似列表、菜单、卡片中复制整套 Tailwind 类。
- 图标优先放到 `src/assets/` 后导入，或复用现有 `ProductIcon`/资源 manifest；不要在 JSX 中手写内联 SVG path。
- 状态和有限取值优先集中到 `src/enums/` 或共享常量，避免业务组件散落魔法字符串。
- 用户可见文案默认中文；涉及多语言区域时同步 `src/i18n/en.json` 并运行 `i18n:check`。不要通过删除翻译键来消除检查错误。
- 保持键盘操作、焦点、语义化标签、可访问名称和移动端行为。交互组件测试优先按 role/name 查询，覆盖点击隔离、禁用态和关键错误态。
- 测试与实现就近放置为 `*.test.ts(x)`；全局测试支持在 `src/test/`。不要用大面积快照代替行为断言。
- `src/api/client.ts` 的 SSE 以空行分帧；任何流式接口变更必须验证分块、尾缓冲、取消和非 2xx 错误。

## 配置、数据库与打包安全

- 配置优先级是操作系统环境变量 → `backend/.env` → `backend/app/config.py` 默认值。根 `.env` 主要供 Compose 使用，两者都不得提交或覆写为示例值。
- SQLite 是零配置/桌面默认；MySQL 服务固定为 MySQL 8.4、`utf8mb4`、UTC。不要删除 bind-mounted 数据，不要把 root 用户当应用账户。
- 对真实数据执行迁移、回填、删除、导入或服务启停属于高影响操作；除非任务明确授权，先停在只读检查和代码/测试层面。
- `packaging/` 修改需同时检查开发态和 PyInstaller frozen 路径，保持产品 slug `gongge-xuban` 与应用标识 `cn.gongge.xuban.desktop`。按修改平台运行相应脚本的语法/静态测试，不要生成或提交签名材料、运行时下载和安装包产物。
- 品牌资源和兼容键受自动扫描约束。不要引入旧品牌字面量或用 allowlist 绕过；兼容读取应集中在既有品牌/存储边界，并由测试证明。

## 文档与交付

- 公共行为、配置、启动命令、目录职责或 API 契约改变时，同步最接近的 README/模块手册；不要把历史计划改写成当前事实。
- 新文件命名、代码语言和术语应沿用所在模块。产品名写作“共格·序伴”，代码 slug 使用 `gongge-xuban`/`gongge_xuban` 的既有场景格式。
- 完成时总结：改了什么、为何这样改、实际验证了什么、仍有哪些明确风险或未运行检查。不要宣称未验证的结果。
