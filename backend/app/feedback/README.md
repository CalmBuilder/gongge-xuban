# 反馈分析与授权材料

本人会话评分、分析和重新分析继续使用原接口。`MessageFeedback.revision` 表示评分/事实修订，`analysis_generation` 表示独立分析任务身份；迟到模型结果只可更新仍匹配的原身份。模型调用在事务外进行，重分析不替换本人已确认的分享内容。

反馈归因复用原 SDK 派发观察器，内部 JSON 修复、429 重试和外层超时重试均在发送前以独立短会话核对原 feedback/tenant/revision/analysis_generation/rating 及本人会话权限。身份失效即结束旧分析，最终条件写回继续保留。仅撤回材料分享不改变合法本人归因身份，不取消已经发出的首请求。

授权材料使用 `/api/enterprise/feedback` 下的接口：

| 方法和相对路径 | 行为 |
| --- | --- |
| `POST /{feedback_id}/materials/preview` | 本人提交 `tenant_id`、`target_agent_id`、`note`、`request_key`，生成服务端不可变预览；生成预览不等于分享 |
| `POST /materials/{material_id}/share` | 本人确认 `tenant_id`、`expected_revision` 和 `material_checksum` 后明确分享 |
| `POST /materials/{material_id}/withdraw` | 本人提交 `tenant_id`、`expected_revision`，永久撤回该身份；回执不返回旧材料正文 |
| `GET /materials` | `tenant_id`、`agent_id` 必填，`mode=owned` 查看本人材料，`mode=managed` 仅读当前目标权限内已分享材料 |
| `GET /materials/{material_id}` | 本人或当前目标管理者读取有效投影；不会因此获得原会话全文权限 |

来源引用原助手回复、用户轮次和实际 Runtime Execution；SOP 包含实例冻结的发布版本 ID/校验和，普通 DTA 不附加 SOP 身份。历史缺失来源明确标记 `missing`，不猜当前技能或最新执行。Operation 投影是**截至该回复的实例回执快照**；`operations_known=false` 表示未知，`true` 与空列表才表示可证明零调用。

默认投影不分享原问答、slots、工具请求/结果/错误或模型分析自由文本。本人说明清洗已知凭据及个人信息模式后展示预览，仍需本人确认；模式清洗不保证识别任意秘密。模型推断只分享固定分类及其非事实提示。

评分修改或删除使旧材料失效，撤回不可重新激活；重新分享须生成新预览身份。同幂等键同参数返回原有效回执，变参数或失效身份返回冲突。权限、事实修订、摘要和状态在当前短事务中重新校验，SQLite 写屏障和 MySQL 当前锁读共用原反馈提交边界。

MySQL 通过 `20261002_0090` Alembic 迁移，SQLite 通过原桌面兼容初始化补列和建表；历史反馈不会自动分享。迁移含活动预览/分享时禁止降级，避免丢失授权状态。

材料可进入[当前 SOP 编辑和候选链](../skills/README.md)，候选隔离与人工发布仍逐次校验材料授权。SD-7 F2/F3 的验收状态以当前进度为准；材料分享不能证明 SOP 已改进，发布成功也不能替代实际业务结果验收。
