# 共格·序伴内置专家快照库

这里保存项目内置 Agency Agents 的本地导入包、中文化包和离线升级证据。它们是项目资产
的版本化输入，不是运行时上游连接器；产品 API、worker、启动流程和定时任务不得读取
GitHub，也不得自动刷新这些目录。

## 目录约定

当前保留的历史/候选包如下：

| 包 | 来源提交 | 用途 |
| --- | --- | --- |
| `agency-agents-import-459dce837db3bdfdc4763d3fefd1fd854e73c8f1/` | `459dce837db3bdfdc4763d3fefd1fd854e73c8f1` | 263 条历史专家导入包 |
| `agency-agents-zh-459dce837db3bdfdc4763d3fefd1fd854e73c8f1/` | `459dce837db3bdfdc4763d3fefd1fd854e73c8f1` | 263 条历史中文化包 |
| `agency-agents-import-3c9588880b7cafaec325a104899fd8bbe27e7d72/` | `3c9588880b7cafaec325a104899fd8bbe27e7d72` | 273 条当前候选导入包 |
| `agency-agents-zh-3c9588880b7cafaec325a104899fd8bbe27e7d72/` | `3c9588880b7cafaec325a104899fd8bbe27e7d72` | 当前候选中文化包 |
| `agency-agents-import/`、`agency-agents-zh/` | 459d... | 既有兼容别名/历史工作目录 |

`agency-agents-import/` 与带 459d 提交号的导入包、`agency-agents-zh/` 与带 459d 提交号的
中文化包的专家文件内容相同，但各自 manifest 的生成批次元数据可能不同；暂时保留原名，
避免已有本地命令或审计材料失效。新批次统一使用带提交号或快照标识的目录，不再在仓库根目录
新增同类目录。

## 升级规则

需要升级时由 Codex 接收用户提供的本地快照/导入包，离线生成差异、风险、许可证、分类、
中文化和 checksum 报告；管理员逐项审核后，才使用 `sync_cli apply` 写入项目数据库。
没有新的本地输入和明确批准时，当前内置专家保持不变。`sync_cli plan/apply/rollback` 在
这里仅表示本地快照迁移、应用和回滚，不表示连接或同步 GitHub。

这些大体积导入产物按仓库忽略规则保留在本机；需要交付某个版本时，应连同 manifest、
报告和 checksum 一起由发布流程显式归档，不要只复制 `experts/` 或 `work/` 子目录。
