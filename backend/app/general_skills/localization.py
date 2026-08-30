"""
@Time       : 2026/08/30 10:30
@Author     : zhanglp8181
@File       : localization.py
@CallChain  : 平台内置 Skill 快照 → revision 中文展示记录 → 管理/开放广场 API；运行时不读取本模块正文
@Description: 管理 Skill 的 revision 级中文名称、描述和解释，校验来源 checksum 后才允许展示。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Protocol

from sqlmodel import Session, select

from app.db.models import (
    GeneralSkill,
    GeneralSkillRevision,
    GeneralSkillRevisionLocalization,
    utc_now,
)


SKILL_LOCALIZATION_LOCALE = "zh-CN"
BUILTIN_SKILL_TRANSLATION_SOURCE = "builtin-summary-v1"


@dataclass(frozen=True, slots=True)
class BuiltinSkillLocalization:
    """保存一个平台内置 Skill 的中文展示摘要，不复制英文运行时正文。"""

    slug: str
    name_zh: str
    description_zh: str
    explanation_markdown_zh: str


class BuiltinSkillCatalogItemLike(Protocol):
    """声明同步中文摘要所需的最小固定快照字段。"""

    catalog_key: str
    slug: str
    content_checksum: str


@dataclass(frozen=True, slots=True)
class BuiltinSkillLocalizationSyncResult:
    """表达一次内置 Skill 中文摘要同步的可审计计数。"""

    created_count: int
    updated_count: int
    stale_count: int
    pending_count: int


def _entry(
    slug: str,
    name_zh: str,
    description_zh: str,
    purpose_zh: str,
    steps: tuple[str, ...],
) -> BuiltinSkillLocalization:
    """把固定中文名称、描述和简要使用流程组装为展示用 Markdown。"""

    step_lines = "\n".join(f"{index}. {step}" for index, step in enumerate(steps, start=1))
    explanation = f"# {name_zh}\n\n{purpose_zh}\n\n## 建议流程\n\n{step_lines}"
    return BuiltinSkillLocalization(
        slug=slug,
        name_zh=name_zh,
        description_zh=description_zh,
        explanation_markdown_zh=explanation,
    )


BUILTIN_SKILL_LOCALIZATIONS: tuple[BuiltinSkillLocalization, ...] = (
    _entry("ask-matt", "向 Matt 提问", "把复杂技术问题整理成可向专家求证的问题。", "适合在遇到技术判断或实现取舍时，先形成清晰问题并获得针对性建议。", ("说明背景、约束和当前尝试。", "明确希望验证的判断或决策。", "记录建议并转化为下一步行动。")),
    _entry("code-review", "代码审查", "从正确性、风险、可维护性和测试覆盖角度审查代码。", "适合对变更集或指定模块进行结构化审查，优先发现会影响行为的缺陷。", ("先确认变更范围和预期行为。", "按风险优先检查实现、边界和回归覆盖。", "输出带证据的发现，并给出修复验证建议。")),
    _entry("codebase-design", "代码库设计", "分析现有代码库并设计可落地的模块与接口方案。", "适合在开始较大改造前建立现状模型、边界和演进路径。", ("定位入口、调用方、数据契约和测试。", "比较候选边界及其对现有行为的影响。", "形成最小可实施设计和验收标准。")),
    _entry("diagnosing-bugs", "故障诊断", "通过复现、证据和最小实验定位软件故障根因。", "适合处理不稳定、难复现或跨模块的错误，避免凭猜测直接修改。", ("收集错误、环境、输入和最近变更。", "建立稳定复现或缩小故障范围。", "验证根因后提出最小修复和回归测试。")),
    _entry("domain-modeling", "领域建模", "把业务概念、关系、状态和规则整理成一致的领域模型。", "适合在数据模型或业务流程复杂时，先消除术语和边界歧义。", ("识别实体、价值对象、关系和生命周期。", "明确不变量、权限边界和状态转换。", "用模型反推接口、存储和测试契约。")),
    _entry("grill-with-docs", "文档驱动深度追问", "依据项目文档连续追问，补齐背景、约束和验收条件。", "适合需求或方案信息不完整时，通过文档证据提高决策质量。", ("先读取相关手册、规范和历史记录。", "针对缺口逐项提问并区分事实与假设。", "汇总已确认结论和仍需决策的事项。")),
    _entry("implement", "实现任务", "将已确认的需求拆成可验证的小步并完成实现。", "适合从明确目标进入编码阶段，保持变更范围和回归路径可控。", ("确认调用方、契约、测试和工作区现状。", "按最小批次实现并及时运行目标检查。", "汇总变更、验证结果和剩余风险。")),
    _entry("improve-codebase-architecture", "改进代码库架构", "在保持行为兼容的前提下改善模块边界、依赖和演进能力。", "适合处理重复逻辑、耦合过深或未来扩展困难的代码结构。", ("用调用链和测试确定真正的架构问题。", "提出有迁移路径的边界调整。", "分批落地并用行为测试守住兼容性。")),
    _entry("prototype", "快速原型", "用最小实现快速验证产品交互、技术可行性或关键假设。", "适合在投入完整工程化前验证方向，原型结论要与正式方案分开记录。", ("选定一个能证明假设的最小场景。", "使用真实边界构建可操作原型。", "记录验证结果、限制和是否进入正式实现。")),
    _entry("research", "研究分析", "围绕问题收集资料、比较证据并形成可追溯结论。", "适合技术选型、竞品分析或不确定性较高的方案判断。", ("定义问题、范围和证据标准。", "优先读取一手资料并记录来源。", "区分事实、推断、风险和推荐行动。")),
    _entry("resolving-merge-conflicts", "解决合并冲突", "安全分析并解决 Git 合并冲突，保留双方有效意图。", "适合代码、配置或文档发生冲突时，先理解语义再完成合并。", ("识别冲突文件和各分支目标。", "按上下文合并并避免覆盖未冲突改动。", "运行相关测试并检查差异后再提交。")),
    _entry("setup-matt-pocock-skills", "设置 Matt Pocock Skills", "检查并配置 Matt Pocock 技能集合的本地使用条件。", "适合初始化技能目录、确认版本和建立可重复的使用环境。", ("确认来源、版本和本地目录。", "检查安装结果与技能文件完整性。", "记录配置方式和后续更新边界。")),
    _entry("tdd", "测试驱动开发", "先用可失败测试定义行为，再以最小实现使其通过。", "适合新功能、缺陷修复和需要明确边界的重构。", ("先写出能复现目标行为的测试。", "实现通过测试所需的最小代码。", "重构并补齐边界、错误和回归场景。")),
    _entry("to-spec", "编写技术规格", "把目标、现状、契约、方案和验收标准整理成技术规格。", "适合在多人协作或跨模块变更前建立共同执行依据。", ("写清问题、非目标和现状证据。", "定义数据、接口、状态和失败路径。", "给出分批实施顺序与可验证验收标准。")),
    _entry("to-tickets", "拆分开发工单", "把技术规格拆成有边界、可独立验收的开发任务。", "适合将复杂方案转为团队可以并行或顺序执行的工作项。", ("按依赖关系划分任务边界。", "为每项写清输入、输出和测试门禁。", "标记风险、阻塞关系和完成定义。")),
    _entry("triage", "问题分诊", "按影响、紧急度、证据和责任边界对问题进行分级。", "适合在多个问题同时出现时先确定处理顺序，避免无序修复。", ("确认问题是否可复现及影响范围。", "区分回归、配置、数据和需求问题。", "分配优先级、负责人和下一次检查点。")),
    _entry("wayfinder", "代码库导航", "快速定位代码库中的入口、模块、契约和相关测试。", "适合接手陌生项目或开始变更前建立可靠的源码地图。", ("从路由、调用方和数据模型开始搜索。", "沿调用链读取实现和相邻测试。", "输出可复用的文件与职责导航。")),
    _entry("wizard", "向导式推进", "通过连续的小步骤引导用户完成复杂任务或决策。", "适合信息需要逐步确认、且每一步都会影响后续路径的工作。", ("先确认当前目标和已知条件。", "一次只推进当前最重要的缺口。", "在关键节点总结并等待下一步输入。")),
    _entry("claude-handoff", "Claude 交接", "把当前工作上下文、结论和待办整理为可交给 Claude 的交接包。", "适合在不同 Agent 或工作会话之间保持上下文连续。", ("汇总目标、已改文件和验证结果。", "列出未完成事项、约束和已知风险。", "提供下一步可直接执行的入口和检查点。")),
    _entry("implement-spec", "按规格实现", "严格依据技术规格完成实现，并在偏差处回到规格决策。", "适合需求已完成设计、需要控制实现漂移的开发任务。", ("逐条映射规格到代码和测试。", "保留版本、错误和兼容契约。", "按验收标准完成回归并记录偏差。")),
    _entry("loop-me", "循环推进", "围绕目标反复执行检查、修复、验证和总结的小闭环。", "适合需要多轮迭代才能收口的开发、调试或文档工作。", ("建立当前轮次的可验证目标。", "执行一次最小改动并检查结果。", "根据证据决定继续、调整或结束。")),
    _entry("retro", "回顾复盘", "对已完成工作复盘事实、决策、问题和可改进流程。", "适合阶段收口或事故后提炼可复用经验，而不是追责式总结。", ("按时间线还原关键事实。", "识别有效做法、失误和根因。", "形成下一轮可执行的改进项。")),
    _entry("setup-ts-deep-modules", "设置 TypeScript 深层模块", "配置和验证 TypeScript 深层模块相关的工程结构与检查。", "适合建立清晰的模块依赖和类型边界，降低跨层耦合。", ("确认 tsconfig、路径和模块入口。", "检查公开 API 与内部实现边界。", "运行类型检查和构建验证配置结果。")),
    _entry("writing-beats", "写作节拍", "把写作目标拆成推进节奏、转折和信息释放的节拍。", "适合需要控制阅读节奏和表达层次的内容创作。", ("明确读者、目标和情绪/信息曲线。", "安排段落节拍与关键转折。", "逐段检查节奏是否支持核心观点。")),
    _entry("writing-fragments", "写作片段", "将零散素材整理成可组合、可继续加工的写作片段。", "适合素材尚未成稿时快速沉淀观点、例子和表达。", ("记录片段的主题和用途。", "保留证据、上下文和待补信息。", "按主题组合并逐步形成完整文章。")),
    _entry("writing-shape", "写作结构", "设计文章、说明或叙事的整体结构与层次。", "适合在展开文字前先建立清晰的内容骨架。", ("明确核心结论和读者路径。", "安排章节、层级和论据顺序。", "用提纲检查覆盖度、重复和跳跃。")),
    _entry("git-guardrails-claude-code", "Git 防护规则（Claude Code）", "为 Claude Code 设定安全的 Git 操作边界和检查规则。", "适合让自动化协作保持分支、差异和提交操作可控。", ("确认仓库状态、分支和用户改动。", "限制高风险 Git 操作并保留确认点。", "提交前检查差异、测试和可回滚性。")),
    _entry("migrate-to-shoehorn", "迁移到 Shoehorn", "按既定迁移路径把项目能力接入 Shoehorn 结构或约定。", "适合进行框架/工具迁移时控制兼容性和增量风险。", ("确认目标版本、映射关系和非目标。", "分批迁移入口、配置、实现和测试。", "用双路径或回归检查确认迁移完成。")),
    _entry("scaffold-exercises", "搭建练习脚手架", "生成用于练习、教学或验证的最小项目脚手架。", "适合创建可重复运行的训练场景和实验入口。", ("确定学习目标和最小文件集合。", "加入可观察的示例与检查点。", "验证脚手架能按说明启动和复现。")),
    _entry("setup-pre-commit", "设置 pre-commit", "配置提交前自动检查，并确保团队可重复执行。", "适合把格式、静态检查和基础质量门禁前移到提交阶段。", ("确认项目语言和现有检查命令。", "配置钩子、版本和依赖来源。", "执行一次全量检查并记录绕过边界。")),
    _entry("grill-me", "追问我", "通过有针对性的追问帮助用户澄清目标、背景和选择。", "适合问题描述模糊或决策依赖隐含信息的场景。", ("先确认最终要解决的问题。", "围绕影响最大的未知逐项追问。", "把回答整理为明确的目标和下一步。")),
    _entry("grilling", "深度追问", "对方案或实现进行系统性压力提问，找出遗漏和脆弱点。", "适合评审前、上线前或高风险决策前的反向检查。", ("覆盖目标、边界、失败和运维问题。", "要求每个关键判断都有证据。", "把发现转为修复项或明确接受的风险。")),
    _entry("handoff", "工作交接", "整理可供下一位协作者继续执行的工作交接信息。", "适合换人、换会话或阶段结束时避免上下文丢失。", ("说明目标、当前状态和已完成工作。", "列出文件、命令、验证和未决事项。", "给出明确的继续入口和完成标准。")),
    _entry("teach", "教学引导", "根据学习者的背景分层解释概念，并用练习确认理解。", "适合把复杂技术或业务知识转化为可跟随的学习路径。", ("先判断已有知识和学习目标。", "从概念到示例再到练习逐步展开。", "根据反馈纠正误解并总结要点。")),
    _entry("to-questionnaire", "转换为问卷", "把开放式需求或访谈内容转成结构化问卷。", "适合批量收集一致信息并降低后续分析成本。", ("确定受访对象和决策目标。", "将问题分组并设计必要选项。", "检查问题顺序、歧义和可统计性。")),
    _entry("wait-what", "澄清与反问", "在关键概念或请求不清楚时暂停并请求最小必要澄清。", "适合避免基于错误假设继续执行，尤其是高影响操作。", ("指出具体不清楚的词或前提。", "提出最少且可回答的澄清问题。", "收到确认后再继续原任务。")),
    _entry("writing-for-agents", "面向 Agent 写作", "编写结构清晰、可执行、便于 Agent 使用的工作指导。", "适合把规则、上下文和完成标准表达成稳定的 Agent 指令。", ("明确上下文入口、目标和信息层级。", "把步骤、边界和完成标准写成可检查规则。", "按渐进披露组织内容并验证 Agent 能正确使用。")),
)

BUILTIN_SKILL_LOCALIZATION_BY_SLUG = {
    item.slug: item for item in BUILTIN_SKILL_LOCALIZATIONS
}


def localization_checksum(
    localized_name: str,
    localized_description: str,
    explanation_markdown: str,
) -> str:
    """按中文展示三元组生成稳定 checksum，检测展示记录是否被意外修改。"""

    payload = json.dumps(
        {
            "name": localized_name,
            "description": localized_description,
            "explanation_markdown": explanation_markdown,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_revision_localization(
    db: Session,
    revision_id: str | None,
    *,
    skill_id: str | None = None,
    locale: str = SKILL_LOCALIZATION_LOCALE,
) -> GeneralSkillRevisionLocalization | None:
    """读取指定修订和语言的展示记录，并可选校验其 Skill 归属。"""

    if not revision_id:
        return None
    statement = select(GeneralSkillRevisionLocalization).where(
        GeneralSkillRevisionLocalization.revision_id == revision_id,
        GeneralSkillRevisionLocalization.catalog_scope == "platform",
        GeneralSkillRevisionLocalization.tenant_id.is_(None),
        GeneralSkillRevisionLocalization.locale == locale,
    )
    if skill_id:
        statement = statement.where(GeneralSkillRevisionLocalization.skill_id == skill_id)
    return db.exec(statement).first()


def is_usable_localization(
    localization: GeneralSkillRevisionLocalization | None,
    revision: GeneralSkillRevision | None,
) -> bool:
    """只有已审核、来源 checksum 和展示 checksum 均匹配时才允许替代英文显示。"""

    if localization is None or revision is None:
        return False
    if localization.translation_status != "verified":
        return False
    if localization.source_content_checksum != revision.content_checksum:
        return False
    return localization.translation_checksum == localization_checksum(
        localization.localized_name,
        localization.localized_description or "",
        localization.explanation_markdown,
    )


def reconcile_builtin_skill_localizations(
    db: Session,
    *,
    catalog_items: Sequence[BuiltinSkillCatalogItemLike],
    actor_user_id: str,
) -> BuiltinSkillLocalizationSyncResult:
    """按固定快照 checksum 幂等同步 37 个平台摘要，源内容变化时降级为待复核。"""

    items_by_catalog_key = {item.catalog_key: item for item in catalog_items}
    skills = db.exec(
        select(GeneralSkill).where(
            GeneralSkill.catalog_scope == "platform",
            GeneralSkill.tenant_id.is_(None),
        )
    ).all()
    created_count = 0
    updated_count = 0
    stale_count = 0
    pending_count = 0
    for skill in skills:
        item = items_by_catalog_key.get(skill.catalog_key or "")
        if item is None:
            continue
        localization_spec = BUILTIN_SKILL_LOCALIZATION_BY_SLUG.get(item.slug)
        if localization_spec is None:
            continue
        revision = db.exec(
            select(GeneralSkillRevision)
            .where(
                GeneralSkillRevision.catalog_scope == "platform",
                GeneralSkillRevision.tenant_id.is_(None),
                GeneralSkillRevision.skill_id == skill.id,
            )
            .order_by(GeneralSkillRevision.revision_number.desc())
        ).first()
        if revision is None:
            continue
        expected_status = "verified" if revision.content_checksum == item.content_checksum else "draft"
        expected_checksum = localization_checksum(
            localization_spec.name_zh,
            localization_spec.description_zh,
            localization_spec.explanation_markdown_zh,
        )
        localization = get_revision_localization(db, revision.id, skill_id=skill.id)
        if localization is None:
            now = utc_now()
            db.add(
                GeneralSkillRevisionLocalization(
                    tenant_id=None,
                    catalog_scope="platform",
                    skill_id=skill.id,
                    revision_id=revision.id,
                    locale=SKILL_LOCALIZATION_LOCALE,
                    localized_name=localization_spec.name_zh,
                    localized_description=localization_spec.description_zh,
                    explanation_markdown=localization_spec.explanation_markdown_zh,
                    translation_status=expected_status,
                    source_content_checksum=revision.content_checksum,
                    translation_checksum=expected_checksum,
                    translation_source=BUILTIN_SKILL_TRANSLATION_SOURCE,
                    created_by=actor_user_id,
                    reviewed_by=actor_user_id if expected_status == "verified" else None,
                    created_at=now,
                    updated_at=now,
                    reviewed_at=now if expected_status == "verified" else None,
                )
            )
            created_count += 1
            if expected_status != "verified":
                pending_count += 1
            continue
        if localization.source_content_checksum != revision.content_checksum:
            if localization.translation_status != "stale":
                localization.translation_status = "stale"
                localization.updated_at = utc_now()
                db.add(localization)
                stale_count += 1
            continue
        changed = False
        for field, value in (
            ("localized_name", localization_spec.name_zh),
            ("localized_description", localization_spec.description_zh),
            ("explanation_markdown", localization_spec.explanation_markdown_zh),
            ("translation_checksum", expected_checksum),
            ("translation_source", BUILTIN_SKILL_TRANSLATION_SOURCE),
        ):
            if getattr(localization, field) != value:
                setattr(localization, field, value)
                changed = True
        if localization.translation_status not in {"verified", "rejected"}:
            if localization.translation_status != expected_status:
                localization.translation_status = expected_status
                changed = True
        if changed:
            localization.updated_at = utc_now()
            db.add(localization)
            updated_count += 1
        if localization.translation_status != "verified":
            pending_count += 1
    db.commit()
    return BuiltinSkillLocalizationSyncResult(
        created_count=created_count,
        updated_count=updated_count,
        stale_count=stale_count,
        pending_count=pending_count,
    )


def revision_for_skill_display(
    db: Session,
    skill: GeneralSkill,
    *,
    pinned_revision_id: str | None = None,
) -> GeneralSkillRevision | None:
    """按绑定固定修订、发布指针或最新修订顺序选择页面应显示的版本。"""

    if pinned_revision_id:
        revision = db.get(GeneralSkillRevision, pinned_revision_id)
        if (
            revision is not None
            and revision.skill_id == skill.id
            and revision.catalog_scope == skill.catalog_scope
            and revision.tenant_id == skill.tenant_id
        ):
            return revision
    if skill.current_published_revision_id:
        revision = db.get(GeneralSkillRevision, skill.current_published_revision_id)
        if (
            revision is not None
            and revision.skill_id == skill.id
            and revision.catalog_scope == skill.catalog_scope
            and revision.tenant_id == skill.tenant_id
        ):
            return revision
    return db.exec(
        select(GeneralSkillRevision)
        .where(
            GeneralSkillRevision.skill_id == skill.id,
            GeneralSkillRevision.catalog_scope == skill.catalog_scope,
            GeneralSkillRevision.tenant_id == skill.tenant_id,
        )
        .order_by(GeneralSkillRevision.revision_number.desc())
    ).first()
