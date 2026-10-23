"""
@Time       : 2026/10/13
@Author     : zhanglp8181
@File       : final_f3_live_memory.py
@CallChain  : 自有F3资源/最终记忆投影 → 原发布与真实SDK → 精确只读记忆旁证
@Description: 13原人工质量作用域的独立适配，仅种合成资源和观察原请求，不授予验收。
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock

from final_f3_preparation import digest
from final_f3_private_jsonl import PrivateJsonlLimitError, append_private_jsonl
from final_f3_sdk_identity import sdk_identity

CASES = {"SOP": ("1", "2", "3", "control", "freeze", "clear"),
         "DTA": ("1", "2", "3", "control", "freeze", "clear", "revoke")}
SLOT_KEYS = ("user_name", "reply_style", "omitted_code")
PREFIX = "本轮输入（仅用于当前调用，不写入对话历史）：\n"
MAX_MEMORY_STATE_ROWS = 256
MAX_MEMORY_STATE_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MEMORY_STATE_SOURCE_BYTES = 512 * 1024
MAX_MEMORY_STATE_JSONL_BYTES = 32 * 1024 * 1024
MAX_MEMORY_STATE_JSONL_FILES = 64
MAX_MEMORY_STATE_JSONL_ROWS = 8192
MAX_MEMORY_STATE_JSONL_ROW_BYTES = 256 * 1024
MAX_MEMORY_STATE_JSONL_ROW_NODES = 50_000
# 当前23案矩阵实测约69K节点；保留2倍余量，仍由32MiB/8192行/50K单行预算兜底。
MAX_MEMORY_STATE_JSONL_NODES = 131_072
MAX_MEMORY_STATE_JSONL_DEPTH = 64
MEMORY_STATE_LIST_KEYS = (
    "executions", "plan_revisions", "signals", "sessions", "records", "jobs", "messages",
    "attention", "sdk_calls",
)
MEMORY_STATE_JSONL_NAMES = ("sdk-observations-*.safe.jsonl", "memory-wire-*.safe.jsonl")


class LiveMemoryError(ValueError):
    """稳定记忆准备错误，不输出私有配置、模型正文或凭据。"""


class LiveMemoryLimitError(LiveMemoryError):
    """私有记忆观察超过固定行数、响应体或审计原件上限时失败关闭。"""


def _state_rows(db, statement) -> list:
    """最多物化257条只读行；超过256时在建立JSON投影前拒绝。"""
    rows = db.exec(statement.limit(MAX_MEMORY_STATE_ROWS + 1)).all()
    if len(rows) > MAX_MEMORY_STATE_ROWS:
        raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_ROW_LIMIT")
    return rows


def _json_string_size(value: str) -> int:
    """逐字符测量JSON字符串的UTF-8展开长度，不生成整字段编码副本。"""
    size = 2
    short_escapes = {'"', "\\", "\b", "\f", "\n", "\r", "\t"}
    for char in value:
        codepoint = ord(char)
        if char in short_escapes:
            size += 2
        elif codepoint < 0x20:
            size += 6
        else:
            try:
                size += len(char.encode("utf-8"))
            except UnicodeEncodeError as error:
                raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_INVALID") from error
    return size


def _bounded_json_size(value: object) -> int:
    """在调用JSONEncoder前有界测量精确响应字节数和对象复杂度。"""
    import math

    size = 0
    visited_nodes = 0
    active_containers: set[int] = set()

    def add(amount: int) -> None:
        """累计精确JSON字节并在超过响应预算时立即中止遍历。"""
        nonlocal size
        size += amount
        if size > MAX_MEMORY_STATE_RESPONSE_BYTES:
            raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_LIMIT")

    def measure(item: object, depth: int) -> None:
        """只遍历JSON原生值，拒绝深层、循环和非JSON自定义对象。"""
        nonlocal visited_nodes
        visited_nodes += 1
        if visited_nodes > 50_000 or depth > 64:
            raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_LIMIT")
        if item is None:
            add(4)
        elif item is True:
            add(4)
        elif item is False:
            add(5)
        elif isinstance(item, str):
            add(_json_string_size(item))
        elif isinstance(item, int):
            if item.bit_length() > 256:
                raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_LIMIT")
            add(len(str(item)))
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_INVALID")
            add(len(repr(item)))
        elif isinstance(item, dict):
            identity = id(item)
            if identity in active_containers:
                raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_INVALID")
            active_containers.add(identity)
            add(2)
            for index, (key, child) in enumerate(item.items()):
                if not isinstance(key, str):
                    raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_INVALID")
                if index:
                    add(1)
                add(_json_string_size(key) + 1)
                measure(child, depth + 1)
            active_containers.remove(identity)
        elif isinstance(item, list):
            identity = id(item)
            if identity in active_containers:
                raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_INVALID")
            active_containers.add(identity)
            add(2)
            for index, child in enumerate(item):
                if index:
                    add(1)
                measure(child, depth + 1)
            active_containers.remove(identity)
        else:
            raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_INVALID")

    measure(value, 0)
    return size


def _state_response_bytes(value: dict) -> bytes:
    """验证九个诊断行集合与2MiB编码上限后才生成HTTP私有读回正文。"""
    if not isinstance(value, dict):
        raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_INVALID")
    for key in MEMORY_STATE_LIST_KEYS:
        rows = value.get(key)
        if not isinstance(rows, list) or len(rows) > MAX_MEMORY_STATE_ROWS:
            raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_ROW_LIMIT")
    _bounded_json_size(value)
    body = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    try:
        for chunk in encoder.iterencode(value):
            encoded = chunk.encode("utf-8")
            if len(body) + len(encoded) > MAX_MEMORY_STATE_RESPONSE_BYTES:
                raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_LIMIT")
            body.extend(encoded)
    except (TypeError, ValueError) as error:
        if isinstance(error, LiveMemoryLimitError):
            raise
        raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_RESPONSE_INVALID") from error
    return bytes(body)


def _state_source_bytes(db, model, conditions: tuple, columns: tuple, current_total: int) -> int:
    """在查询文本/JSON字段之前聚合数据库字节数，超过总预算即拒绝物化。"""
    from sqlalchemy import LargeBinary, cast, func, select

    expressions = [func.coalesce(func.length(cast(column, LargeBinary)), 0) for column in columns]
    row_size = expressions[0]
    for expression in expressions[1:]:
        row_size = row_size + expression
    total_row = db.exec(select(func.coalesce(func.sum(row_size), 0))
        .select_from(model).where(*conditions)).one()
    combined = current_total + int(total_row[0] or 0)
    if combined > MAX_MEMORY_STATE_SOURCE_BYTES:
        raise LiveMemoryLimitError("F3_LIVE_MEMORY_SOURCE_LIMIT")
    return combined


def _preflight_jsonl_row(chunks: bytearray, start: int, end: int, total_nodes: int) -> int:
    """单行限制5万节点、运行合计限制131072节点后才允许复制并解析JSONL。"""
    nodes = 0
    depth = 0
    in_string = False
    escaped = False
    in_primitive = False

    for position in range(start, end):
        value = chunks[position]
        if in_string:
            if escaped:
                escaped = False
            elif value == 0x5C:
                escaped = True
            elif value == 0x22:
                in_string = False
            continue

        if value in (0x20, 0x09, 0x0A, 0x0D):
            in_primitive = False
            continue
        if value == 0x22:
            nodes += 1
            in_string = True
            in_primitive = False
        elif value in (0x7B, 0x5B):
            nodes += 1
            depth += 1
            in_primitive = False
            if depth > MAX_MEMORY_STATE_JSONL_DEPTH:
                raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_JSONL_COMPLEXITY_LIMIT")
        elif value in (0x7D, 0x5D):
            nodes += 1
            depth -= 1
            in_primitive = False
            if depth < 0:
                raise LiveMemoryError("F3_LIVE_MEMORY_STATE_JSONL_INVALID")
        elif value in (0x2C, 0x3A):
            nodes += 1
            in_primitive = False
        elif not in_primitive:
            nodes += 1
            in_primitive = True

        if nodes > MAX_MEMORY_STATE_JSONL_ROW_NODES or total_nodes + nodes > MAX_MEMORY_STATE_JSONL_NODES:
            raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_JSONL_COMPLEXITY_LIMIT")

    return total_nodes + nodes


def _read_bounded_jsonl(runtime: Path) -> tuple[list[dict], list[dict]]:
    """安全读取私有SDK/完整事实JSONL，并在解析前执行文件数、字节数和行数上限。"""
    _private_runtime(runtime)
    paths = sorted({path for pattern in MEMORY_STATE_JSONL_NAMES for path in runtime.glob(pattern)})
    if len(paths) > MAX_MEMORY_STATE_JSONL_FILES:
        raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_FILE_LIMIT")
    total_bytes = 0
    total_rows = 0
    total_nodes = 0
    groups: list[list[dict]] = [[], []]
    for index, pattern in enumerate(MEMORY_STATE_JSONL_NAMES):
        for path in sorted(runtime.glob(pattern)):
            descriptor = -1
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                before = os.fstat(descriptor)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                        or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600):
                    raise LiveMemoryError("F3_LIVE_MEMORY_PRIVATE_FILE_REQUIRED")
                if (before.st_size < 0 or before.st_size > MAX_MEMORY_STATE_JSONL_BYTES
                        or total_bytes + before.st_size > MAX_MEMORY_STATE_JSONL_BYTES):
                    raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_JSONL_BYTE_LIMIT")
                remaining = before.st_size
                chunks = bytearray()
                while remaining:
                    chunk = os.read(descriptor, min(65_536, remaining))
                    if not chunk:
                        raise LiveMemoryError("F3_LIVE_MEMORY_STATE_JSONL_READ_INVALID")
                    chunks.extend(chunk)
                    remaining -= len(chunk)
                after = os.fstat(descriptor)
                if (after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns
                        or len(chunks) != before.st_size):
                    raise LiveMemoryError("F3_LIVE_MEMORY_STATE_JSONL_CHANGED")
                total_bytes += len(chunks)
                line_start = 0
                while line_start < len(chunks):
                    newline = chunks.find(b"\n", line_start)
                    line_end = len(chunks) if newline < 0 else newline
                    if line_end > line_start and chunks[line_end - 1] == 0x0D:
                        line_end -= 1
                    if line_end > line_start:
                        total_rows += 1
                        if total_rows > MAX_MEMORY_STATE_JSONL_ROWS:
                            raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_ROW_LIMIT")
                        if line_end - line_start > MAX_MEMORY_STATE_JSONL_ROW_BYTES:
                            raise LiveMemoryLimitError("F3_LIVE_MEMORY_STATE_JSONL_ROW_BYTE_LIMIT")
                        total_nodes = _preflight_jsonl_row(
                            chunks, line_start, line_end, total_nodes
                        )
                        row = json.loads(bytes(chunks[line_start:line_end]))
                        if not isinstance(row, dict):
                            raise LiveMemoryError("F3_LIVE_MEMORY_STATE_JSONL_INVALID")
                        groups[index].append(row)
                    if newline < 0:
                        break
                    line_start = newline + 1
            except LiveMemoryError:
                raise
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise LiveMemoryError("F3_LIVE_MEMORY_STATE_JSONL_INVALID") from error
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
    return groups[0], groups[1]


def validate_memory_quality(spec: dict) -> dict:
    """验证准确13员工及原固定人工标签，拒绝合并业务作用域或预填成功。"""
    try:
        quality = spec["memory_quality"]
        if set(quality) != {"agents", "labels", "skill_prefix"} or set(quality["agents"]) != set(CASES):
            raise ValueError
        identities = {value["id"] for value in spec.get("agents", {}).values()}
        names = set()
        for branch, cases in CASES.items():
            if set(quality["agents"][branch]) != set(cases):
                raise ValueError
            for item in quality["agents"][branch].values():
                if set(item) != {"id", "name"} or not all(
                    isinstance(item[key], str) and item[key].strip() for key in ("id", "name")
                ) or len(item["id"]) > 128 or item["id"] in identities or item["name"] in names:
                    raise ValueError
                identities.add(item["id"])
                names.add(item["name"])
        labels = quality["labels"]
        if (set(labels) != {"name", "style", "large"} or labels["name"] != "青禾"
                or labels["style"] != "输出偏好：简洁要点；原样保留代码 a  b\n\t校验结束。"
                or labels["large"] != "口令：F3-UNSELECTED-SECRET。" + "完整长事实。" * 700):
            raise ValueError
        if not re.fullmatch(r"f3_memory_[a-z0-9_]+", quality["skill_prefix"]):
            raise ValueError
        if not all(isinstance(spec[section]["id"], str) and spec[section]["id"].strip()
                   for section in ("tenant", "owner", "reviewer")):
            raise ValueError
        if spec["owner"]["id"] == spec["reviewer"]["id"]:
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise LiveMemoryError("F3_LIVE_MEMORY_SPEC_INVALID") from error
    return deepcopy(quality)


def _private_runtime(runtime: Path) -> None:
    """运行根必须是当前UID的700真实目录，不能通过链接写入其他位置。"""
    try:
        info = runtime.lstat()
        if (runtime.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700 or runtime.resolve() != runtime):
            raise ValueError
    except (OSError, ValueError) as error:
        raise LiveMemoryError("F3_LIVE_MEMORY_PRIVATE_RUNTIME_REQUIRED") from error


def _isolated_database(db) -> None:
    """写资源只允许显式本次自有SQLite或随机原生MySQL的最小账号。"""
    from sqlalchemy.engine import make_url

    runtime = Path(os.environ.get("F3_LIVE_RUNTIME", ""))
    _private_runtime(runtime)
    actual = db.get_bind().url
    if (os.environ.get("FULLSTACK_FINAL_F3_LIVE") != "1"
            or actual != make_url(os.environ.get("DATABASE_URL", ""))):
        raise LiveMemoryError("F3_LIVE_MEMORY_ISOLATION_REQUIRED")
    if actual.get_backend_name() == "sqlite":
        if actual.database != str(runtime / "journey.sqlite3"):
            raise LiveMemoryError("F3_LIVE_MEMORY_ISOLATION_REQUIRED")
    elif (actual.get_backend_name() != "mysql" or actual.host != "127.0.0.1"
          or actual.port in (None, 3306) or not (actual.database or "").startswith("gongge_xuban_test_")
          or not (actual.username or "").startswith("st_test_")):
        raise LiveMemoryError("F3_LIVE_MEMORY_ISOLATION_REQUIRED")


def seed(db, spec: dict) -> dict:
    """仅首次种13员工和39完整事实，SOP与DTA均走原publisher，全部运行账本保持空。"""
    from app.api.skills import create_skill
    from app.db.models import AgentProfile, AgentUsage, MemoryRecord, User
    from app.skills.skill_schema import SkillCard, SkillCreateRequest

    quality = validate_memory_quality(spec)
    _isolated_database(db)
    tenant, owner_id = spec["tenant"]["id"], spec["owner"]["id"]
    owner = db.get(User, owner_id)
    reviewer = db.get(User, spec["reviewer"]["id"])
    if (owner is None or reviewer is None or owner.tenant_id != tenant or reviewer.tenant_id != tenant
            or owner.membership_status != "active" or reviewer.membership_status != "active"
            or owner.role != "admin" or reviewer.role != "admin"):
        raise LiveMemoryError("F3_LIVE_MEMORY_ISOLATION_REQUIRED")
    if any(db.get(AgentProfile, item["id"]) is not None
           for cases in quality["agents"].values() for item in cases.values()):
        raise LiveMemoryError("F3_LIVE_MEMORY_SEED_EXISTS")
    memory_ids = {}
    for branch, cases in quality["agents"].items():
        memory_ids[branch] = {}
        for case, item in cases.items():
            agent_owner = reviewer.id if case in {"freeze", "revoke"} else owner.id
            db.add(AgentProfile(**item, tenant_id=tenant, status="active", owner_user_id=agent_owner,
                published_to_gallery=True, visibility_scope="tenant",
                description="本轮独立完整记忆人工标注员工。",
                metadata_json={"owner_user_id": agent_owner}))
            db.flush()
            db.add(AgentUsage(tenant_id=tenant, agent_id=item["id"], user_id=owner.id))
            ids = {}
            for suffix, kind, key in (("name", "profile", "preferred_name"),
                                      ("style", "preference", "reply_style"),
                                      ("large", "fact", "reference_code")):
                identity = "mem_f3_" + digest(item["id"])[:24] + "_" + suffix
                db.add(MemoryRecord(id=identity, tenant_id=tenant, user_id=owner.id,
                    username=owner.username, agent_id=item["id"], agent_scope_key=item["id"],
                    kind=kind, content=quality["labels"][suffix], importance=0.9,
                    metadata_json={"key": key}))
                ids[suffix] = identity
            memory_ids[branch][case] = ids
    db.commit()
    published = {}
    for case, item in quality["agents"]["SOP"].items():
        description = (
            "办理记忆核验流程；Router填写user_name、reply_style、omitted_code。"
            "user_name为实际可用姓名。reply_style的值是冒号后的正文关键词，不能写字段标签“输出偏好”或整条正文；"
            "本轮明确纠正优先，未知格式填未知。"
            "omitted_code只填实际投影已知口令，未知口令填未知。"
            "不能把用户要求的关键词等格式要求当作字段事实。"
        )
        fields = {"user_name": {"type": "string", "description": "实际可用姓名"},
                  "reply_style": {"type": "string", "description":
                      "值是冒号后的正文关键词，不能写字段标签“输出偏好”或整条正文；"
                      "本轮纠正优先，未知填未知。"},
                  "omitted_code": {"type": "string", "description":
                      "仅实际投影中已知口令；无可用事实填未知，不推断未投影条目。"}}
        style_aliases = {"未知": "未知"}
        if case != "control":
            for canonical in ("简洁要点", "详细表格"):
                style_aliases[canonical] = canonical
                style_aliases[canonical + "；原样保留代码 a  b"] = canonical
        nodes = [{"node_id": "collect", "type": "collect_input", "name": "核对记忆字段",
                  "expected_user_info": list(SLOT_KEYS), "allowed_actions": ["ask_user", "continue_flow"],
                  "metadata": {"value_aliases": {"reply_style": style_aliases}}}]
        edges = [{"source_node_id": "collect", "next_node_id": "done"}]
        terminal_node_ids = ["done"]
        if case in {"freeze", "clear"}:
            nodes.append({"node_id": "confirm", "type": "collect_input", "name": "本人确认冻结记忆",
                          "expected_user_info": ["confirmed"], "allowed_actions": ["ask_user", "continue_flow"],
                          "metadata": {"confirmation_policy": {
                              "slot_name": "confirmed", "phrase_values": {"确认记忆核验": "yes"},
                              "prompt": "请回复确认记忆核验；此前只保留当前冻结计划。"}}})
            nodes.append({"node_id": "done", "type": "terminal", "name": "核验完成",
                          "allowed_actions": ["answer_user"]})
            edges = [{"source_node_id": "collect", "next_node_id": "confirm"},
                     {"source_node_id": "confirm", "next_node_id": "done"}]
        else:
            nodes.append({"node_id": "done", "type": "terminal", "name": "核验完成",
                          "allowed_actions": ["answer_user"]})
        card = SkillCard.model_validate({"skill_id": quality["skill_prefix"] + "_" + case,
            "name": "完整记忆核验流程", "version": "1.0.0", "execution_mode": "deterministic",
            "description": description, "required_info": list(SLOT_KEYS),
            "trigger_intents": ["办理完整记忆核验流程"],
            "condition_schemas": {"slots": {"type": "object", "properties": fields}},
            "nodes": nodes, "edges": edges,
            "start_node_id": "collect", "terminal_node_ids": terminal_node_ids})
        result = create_skill(SkillCreateRequest(tenant_id=tenant, content=card, status="published"),
                              agent_id=item["id"], db=db, current_user=owner)
        published[case] = {"skill_id": result.skill_id, "version": result.version}
    return {**quality, "agents": {branch: {case: {**item, "tenant_id": tenant,
            "gallery_label": item["name"] + " @" + (
                reviewer.id if case in {"freeze", "revoke"} else owner.id
            )} for case, item in cases.items()}
            for branch, cases in quality["agents"].items()}, "memory_ids": memory_ids,
            "published_skills": published, "real_vendor_qualified": False,
            "real_model_quality_passed": False}


def _labeled_fields(markdown: str) -> dict:
    """完整字段值严格解析；同键冲突不能被后一个值覆盖为通过。"""
    pattern = (r"\b(NAME|STYLE|CODE)\b[\s*`]*=[\s*`]*([^\r\n]+?)"
               r"(?=(?:\s+(?:与|and)\s+|[，,、；;]\s*|\s+)[*`]*(?:NAME|STYLE|CODE)"
               r"[\s*`]*=|[\r\n]|$)")
    matches = [*re.findall(pattern, markdown),
               *re.findall(r"^\s*\|[\s*`]*(NAME|STYLE|CODE)[\s*`]*\|[\s*`]*([^|\r\n]+)\|", markdown, re.M)]
    labels = {}
    for key, item in matches:
        value = re.sub(r"[\s*`]+$", "", item.strip())
        labels[key] = ("__CONFLICTING_LABEL_VALUES__"
                       if key in labels and labels[key] != value else value)
    return labels


def _selected(snapshot: dict, validate) -> list:
    """原快照选择只读并独立核当前披露权限，不重新从管理记忆补选择。"""
    from app.memory.projection import MemoryCandidate

    return [{"id": item["id"], "hash": hashlib.sha256(item["content"].encode()).hexdigest(),
             "currently_allowed": validate(MemoryCandidate.restore(item))}
            for item in snapshot.get("selected", [])]


def state(db, spec: dict, agent_id: str, *, session_id: str | None = None) -> dict:
    """精确只读13白名单的原实例、槽位、冻结选择、真实捕获来源与SDK旁证。"""
    from sqlmodel import select
    from app.db.models import (
        ChatSession, ExecutionPlanRevision, ExecutionSignal, MemoryCaptureJob, MemoryRecord,
        Message, SkillVersion, SopInstance, SopWorkItem,
    )
    from app.memory.service import MemoryService

    quality = validate_memory_quality(spec)
    agents = [item["id"] for cases in quality["agents"].values() for item in cases.values()]
    tenant, owner = spec["tenant"]["id"], spec["owner"]["id"]
    if agent_id not in agents:
        raise LiveMemoryError("F3_LIVE_MEMORY_SCOPE_FORBIDDEN")
    session_conditions = [ChatSession.tenant_id == tenant,
        ChatSession.user_id == owner, ChatSession.agent_id == agent_id]
    if session_id:
        session_conditions.append(ChatSession.id == session_id)
    source_bytes = _state_source_bytes(db, ChatSession, tuple(session_conditions),
        (ChatSession.id, ChatSession.tenant_id, ChatSession.user_id, ChatSession.agent_id,
         ChatSession.slots_json, ChatSession.context_state_json), 0)
    session_ids = _state_rows(db, select(ChatSession.id).where(*session_conditions))
    if session_id and not session_ids:
        raise LiveMemoryError("F3_LIVE_MEMORY_SCOPE_FORBIDDEN")
    session_ids = list(session_ids)
    sessions = _state_rows(db, select(ChatSession.id, ChatSession.tenant_id, ChatSession.user_id,
        ChatSession.agent_id, ChatSession.slots_json, ChatSession.context_state_json)
        .where(*session_conditions))
    instance_conditions = [SopInstance.tenant_id == tenant, SopInstance.agent_id == agent_id,
        SopInstance.initiator_user_id == owner, SopInstance.session_id.in_(session_ids)]
    if session_ids:
        source_bytes = _state_source_bytes(db, SopInstance, tuple(instance_conditions),
            (SopInstance.id, SopInstance.kind, SopInstance.status, SopInstance.tenant_id,
             SopInstance.agent_id, SopInstance.initiator_user_id, SopInstance.session_id,
             SopInstance.current_plan_checksum, SopInstance.current_plan_revision_id,
             SopInstance.source_ref, SopInstance.skill_id, SopInstance.skill_version_id,
             SopInstance.skill_version, SopInstance.definition_checksum,
             SopInstance.slots_json, SopInstance.context_json), source_bytes)
        instances = _state_rows(db, select(SopInstance.id, SopInstance.kind, SopInstance.status,
            SopInstance.tenant_id, SopInstance.agent_id, SopInstance.initiator_user_id,
            SopInstance.session_id, SopInstance.current_plan_checksum,
            SopInstance.current_plan_revision_id, SopInstance.source_ref, SopInstance.skill_id,
            SopInstance.skill_version_id, SopInstance.skill_version, SopInstance.definition_checksum,
            SopInstance.slots_json, SopInstance.context_json).where(*instance_conditions)
            .order_by(SopInstance.created_at, SopInstance.id))
    else:
        instances = []
    record_conditions = [MemoryRecord.tenant_id == tenant, MemoryRecord.user_id == owner,
        MemoryRecord.agent_scope_key.in_([*agents, "__user__"])]
    source_bytes = _state_source_bytes(db, MemoryRecord, tuple(record_conditions),
        (MemoryRecord.id, MemoryRecord.kind, MemoryRecord.content,
         MemoryRecord.metadata_json, MemoryRecord.capture_job_id), source_bytes)
    validate = MemoryService(db).projection_validator(tenant, owner, agent_id=agent_id)
    executions = []
    for item in instances:
        context = item.context_json or {}
        frozen = context.get("memory_projection") or {}
        active = context.get("memory_projection_active") or frozen
        executions.append({"id": item.id, "kind": item.kind, "status": item.status,
            "tenant_id": item.tenant_id, "agent_id": item.agent_id, "initiator_user_id": item.initiator_user_id,
            "session_id": item.session_id, "plan_checksum": item.current_plan_checksum,
            "current_plan_revision_id": item.current_plan_revision_id,
            "source_input_id": context.get("source_input_id") or item.source_ref,
            "skill_version_id": item.skill_version_id, "definition_checksum": item.definition_checksum,
            "slots": {key: (item.slots_json or {}).get(key) for key in SLOT_KEYS},
            "selected": _selected(frozen, validate), "active_selected": _selected(active, validate),
            "projection_policy": frozen.get("policy_version")})
    records = _state_rows(db, select(MemoryRecord.id, MemoryRecord.kind, MemoryRecord.content,
        MemoryRecord.source_sequence, MemoryRecord.capture_job_id).where(
            MemoryRecord.tenant_id == tenant, MemoryRecord.user_id == owner,
            MemoryRecord.agent_id == agent_id))
    job_conditions = [MemoryCaptureJob.tenant_id == tenant, MemoryCaptureJob.user_id == owner,
        MemoryCaptureJob.agent_id == agent_id]
    source_bytes = _state_source_bytes(db, MemoryCaptureJob, tuple(job_conditions),
        (MemoryCaptureJob.id, MemoryCaptureJob.tenant_id, MemoryCaptureJob.user_id,
         MemoryCaptureJob.agent_id, MemoryCaptureJob.status, MemoryCaptureJob.source_message_id,
         MemoryCaptureJob.source_assistant_message_id), source_bytes)
    jobs = {job.id: job for job in _state_rows(db, select(MemoryCaptureJob.id,
        MemoryCaptureJob.tenant_id, MemoryCaptureJob.user_id, MemoryCaptureJob.agent_id,
        MemoryCaptureJob.status, MemoryCaptureJob.source_message_id,
        MemoryCaptureJob.source_assistant_message_id, MemoryCaptureJob.source_sequence)
        .where(*job_conditions))}
    if session_ids:
        message_conditions = [Message.tenant_id == tenant, Message.session_id.in_(session_ids)]
        source_bytes = _state_source_bytes(db, Message, tuple(message_conditions),
            (Message.id, Message.role, Message.tenant_id, Message.session_id,
             Message.content, Message.metadata_json), source_bytes)
        messages = _state_rows(db, select(Message.id, Message.role, Message.tenant_id,
            Message.session_id, Message.content, Message.metadata_json).where(*message_conditions)
            .order_by(Message.created_at, Message.id))
    else:
        messages = []
    execution_ids = [item.id for item in instances]
    if execution_ids:
        plan_conditions = [ExecutionPlanRevision.tenant_id == tenant,
            ExecutionPlanRevision.execution_id.in_(execution_ids)]
        source_bytes = _state_source_bytes(db, ExecutionPlanRevision, tuple(plan_conditions),
            (ExecutionPlanRevision.id, ExecutionPlanRevision.tenant_id,
             ExecutionPlanRevision.execution_id, ExecutionPlanRevision.parent_revision_id,
             ExecutionPlanRevision.checksum, ExecutionPlanRevision.status), source_bytes)
        plans = _state_rows(db, select(ExecutionPlanRevision.id, ExecutionPlanRevision.tenant_id,
            ExecutionPlanRevision.execution_id, ExecutionPlanRevision.parent_revision_id,
            ExecutionPlanRevision.checksum, ExecutionPlanRevision.status).where(*plan_conditions))
        signal_conditions = [ExecutionSignal.tenant_id == tenant,
            ExecutionSignal.execution_id.in_(execution_ids)]
        source_bytes = _state_source_bytes(db, ExecutionSignal, tuple(signal_conditions),
            (ExecutionSignal.id, ExecutionSignal.tenant_id, ExecutionSignal.execution_id,
             ExecutionSignal.signal_type, ExecutionSignal.status,
             ExecutionSignal.causation_id), source_bytes)
        signals = _state_rows(db, select(ExecutionSignal.id, ExecutionSignal.tenant_id,
            ExecutionSignal.execution_id, ExecutionSignal.signal_type, ExecutionSignal.status,
            ExecutionSignal.causation_id, ExecutionSignal.consumed_at).where(*signal_conditions))
    else:
        plans, signals = [], []
    version_ids = [item.skill_version_id for item in instances if item.skill_version_id]
    if version_ids:
        version_conditions = [SkillVersion.tenant_id == tenant, SkillVersion.id.in_(version_ids)]
        source_bytes = _state_source_bytes(db, SkillVersion, tuple(version_conditions),
            (SkillVersion.id, SkillVersion.tenant_id, SkillVersion.skill_id,
             SkillVersion.version, SkillVersion.compiled_definition_checksum), source_bytes)
        versions = _state_rows(db, select(SkillVersion.id, SkillVersion.tenant_id,
            SkillVersion.skill_id, SkillVersion.version,
            SkillVersion.compiled_definition_checksum).where(*version_conditions))
    else:
        versions = []
    if execution_ids:
        attention_conditions = [SopWorkItem.tenant_id == tenant,
            SopWorkItem.instance_id.in_(execution_ids)]
        source_bytes = _state_source_bytes(db, SopWorkItem, tuple(attention_conditions),
            (SopWorkItem.id, SopWorkItem.instance_id, SopWorkItem.attention_kind,
             SopWorkItem.status, SopWorkItem.title, SopWorkItem.allowed_commands_json), source_bytes)
        attention = _state_rows(db, select(SopWorkItem.id, SopWorkItem.instance_id,
            SopWorkItem.attention_kind, SopWorkItem.status, SopWorkItem.revision,
            SopWorkItem.title, SopWorkItem.allowed_commands_json).where(*attention_conditions))
    else:
        attention = []
    sdk_calls = _sdk_calls(Path(os.environ.get("F3_LIVE_RUNTIME", "")), tenant, owner, agent_id,
        session_ids, execution_ids, messages, execution_rows=instances, session_rows=sessions,
        plans=plans, signals=signals, versions=versions, memory_jobs=list(jobs.values()))
    from final_f3_live_fixture import _fields

    return {"pid": os.getpid(), "tenant_id": tenant, "user_id": owner, "agent_id": agent_id,
        "executions": executions,
        "plan_revisions": [_fields(row, ("id", "tenant_id", "execution_id", "parent_revision_id",
                                         "checksum", "status")) for row in plans],
        "signals": [_fields(row, ("id", "tenant_id", "execution_id", "signal_type", "status",
                                  "causation_id", "consumed_at")) for row in signals],
        "sessions": [{"id": item.id, "slots": {key: (item.slots_json or {}).get(key) for key in SLOT_KEYS},
            "router_audit": (item.context_state_json or {}).get("memory_projection_audit", [])}
            for item in sessions],
        "records": [{"id": item.id, "kind": item.kind, "characters": len(item.content),
            "source_sequence": item.source_sequence, "capture_job_id": item.capture_job_id,
            "source_input_id": jobs[item.capture_job_id].source_message_id
                if item.capture_job_id in jobs else None,
            "hash": hashlib.sha256(item.content.encode()).hexdigest()} for item in records],
        "jobs": [{"id": item.id, "tenant_id": item.tenant_id, "user_id": item.user_id,
            "agent_id": item.agent_id, "status": item.status, "source_message_id": item.source_message_id,
            "source_assistant_message_id": item.source_assistant_message_id,
            "source_sequence": item.source_sequence} for item in jobs.values()],
        "messages": [{"id": item.id, "role": item.role, "tenant_id": item.tenant_id,
            "session_id": item.session_id,
            "content_sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
            "labels": _labeled_fields(item.content) if item.role == "assistant" else {},
            **{key: (item.metadata_json or {}).get(key) for key in
               ("client_turn_id", "user_message_id", "turn_id", "execution_id", "result_id")}}
            for item in messages],
        "attention": [{"id": item.id, "execution_id": item.instance_id, "kind": item.attention_kind,
            "status": item.status, "revision": item.revision, "title": item.title,
            "allowed_commands": item.allowed_commands_json} for item in attention],
        "sdk_calls": sdk_calls, "real_vendor_qualified": False, "real_model_quality_passed": False}


def _sdk_calls(runtime: Path, tenant: str, owner: str, agent: str,
               sessions: list, instances: list, messages: list, *, execution_rows=(),
               session_rows=(), plans=(), signals=(), versions=(), memory_jobs=()) -> list:
    """用原SDK receipt/PID/请求hash交叉只读旁证，缺身份或原传输证据保持未验。"""
    from final_f3_live_fixture import _persistent_sdk_bindings

    identity_rows = {"tenant_id": [tenant], "user_id": [owner], "agent_id": [agent],
        "session_id": sessions, "instance_id": instances,
        "source_input_id": [item.id for item in messages if item.role == "user"],
        "turn_id": [item.id for item in messages if item.role == "user"],
        "task_id": [item.id for item in memory_jobs]}
    phases = ("router.scene", "dynamic_task.plan", "dynamic_task.answer",
              "dynamic_task.answer_repair", "memory.capture")
    receipts, wires = _read_bounded_jsonl(runtime)
    result = []
    consumed = set()
    for wire in wires:
        hashes = wire.get("identity_sha256") or {}
        if any(hashes.get(key) != digest(value) for key, value in
               (("tenant_id", tenant), ("user_id", owner), ("agent_id", agent))):
            continue
        matches = [(index, item) for index, item in enumerate(receipts)
            if index not in consumed and item.get("request_sha256") == wire.get("sdk_request_sha256")
            and item.get("pid") == wire.get("pid") and item.get("identity_sha256") == hashes]
        receipt = matches[0][1] if matches else {}
        if matches:
            consumed.add(matches[0][0])
        bindings = {key: next((item for item in values if digest(item) == hashes.get(key)), None)
                    for key, values in identity_rows.items()}
        bindings, binding_valid = _persistent_sdk_bindings(hashes, bindings,
            tenant=tenant, owner=owner, instances=execution_rows, sessions=session_rows,
            messages=messages, plans=plans, signals=signals, versions=versions,
            memory_jobs=memory_jobs)
        result.append({**wire, **bindings, "call_id": receipt.get("call_id"),
            "status": receipt.get("status"),
            "usage": receipt.get("usage") or {}, "operation": next((phase for phase in phases
            if digest(phase) == wire.get("phase_sha256")), None),
            "actual_sdk_transport": receipt.get("observation_valid") is True,
            "actual_provider_call": wire.get("actual_provider_call") is True
                and receipt.get("observation_valid") is True,
            "original_sdk_receipt_found": bool(matches),
            "persistent_binding_valid": bool(matches) and binding_valid})
    return result


def install_api(application, runtime, spec: dict, *, session_factory=None) -> None:
    """仅自有运行根安装私有key授权的GET，不添加模型/修改/恢复入口。"""
    from fastapi import Header, HTTPException
    from fastapi.responses import Response

    _private_runtime(Path(runtime))
    if (os.environ.get("FULLSTACK_FINAL_F3_LIVE") != "1"
            or os.environ.get("F3_LIVE_RUNTIME") != str(runtime)):
        raise LiveMemoryError("F3_LIVE_MEMORY_ISOLATION_REQUIRED")
    validate_memory_quality(spec)
    if session_factory is None:
        from sqlmodel import Session
        from app.db import engine

        def session_factory():
            """每个原只读请求使用独立短会话，不保留旧权限快照。"""
            return Session(engine)

    @application.get("/__e2e/f3/memory/{agent_id}")
    def memory_state(agent_id: str, session_id: str | None = None,
                     x_f3_control_key: str | None = Header(None)):
        """私有授权先于数据库读取，未知scope拒绝且响应禁止缓存。"""
        if not x_f3_control_key or not hmac.compare_digest(x_f3_control_key, spec["control_key"]):
            raise HTTPException(status_code=403, detail="F3_LIVE_MEMORY_CONTROL_FORBIDDEN")
        try:
            with session_factory() as db:
                result = state(db, spec, agent_id, session_id=session_id)
            body = _state_response_bytes(result)
        except LiveMemoryLimitError as error:
            raise HTTPException(status_code=413, detail="F3_LIVE_MEMORY_STATE_LIMIT") from error
        except LiveMemoryError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return Response(content=body, media_type="application/json",
                        headers={"Cache-Control": "no-store"})


def _strings(value):
    """只解析原JSON/固定本轮前缀，保留完整换行双空格，不搜索自由文本JSON。"""
    if isinstance(value, str):
        yield value
        text = value[len(PREFIX):] if value.startswith(PREFIX) else value
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return
        if isinstance(parsed, (dict, list)):
            yield from _strings(parsed)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


class MemoryWireObserver:
    """原最终SDK完整事实与usage安全观察，不替代原SDK账本或授予业务质量。"""

    def __init__(self, runtime):
        """持有自有私有运行根，不读取模型配置或建立传输。"""
        self.runtime = Path(runtime)
        _private_runtime(self.runtime)
        self.path = self.runtime / f"memory-wire-{os.getpid()}.safe.jsonl"
        self._lock = RLock()
        self._captures = {}
        self._count = 0
        self._errors = 0

    def _key(self, messages):
        """按实际messages、原安全身份和PID配对，不把上次来源借给本次调用。"""
        return digest(messages), digest(sdk_identity().get("identity_sha256", {})), os.getpid()

    def capture(self, payload, messages):
        """原渲染返回后留内存选择和审计，实际create发生才落安全旁证。"""
        from app.memory.request_projection import MemoryRequestPayload

        if not isinstance(payload, MemoryRequestPayload):
            return
        envelope = payload.envelope
        selected = tuple(envelope.selected or ())
        revoked = tuple(item.content for item in envelope.candidates
                        if envelope.validate is not None and not envelope.validate(item))
        with self._lock:
            self._captures[self._key(messages)] = (selected, revoked,
                deepcopy(envelope.audits[-1]) if envelope.audits else {})

    def _append(self, row):
        """只追加自有600单链接普通文件，正文/模型凭据均不写日志。"""
        _private_runtime(self.runtime)
        try:
            append_private_jsonl(
                self.runtime, self.path, row,
                maximum_bytes=MAX_MEMORY_STATE_JSONL_BYTES,
                maximum_rows=MAX_MEMORY_STATE_JSONL_ROWS,
                maximum_files=MAX_MEMORY_STATE_JSONL_FILES,
                patterns=MEMORY_STATE_JSONL_NAMES,
                row_limit_code="F3_LIVE_MEMORY_STATE_ROW_LIMIT",
                byte_limit_code="F3_LIVE_MEMORY_STATE_JSONL_BYTE_LIMIT",
                file_limit_code="F3_LIVE_MEMORY_STATE_FILE_LIMIT",
            )
        except PrivateJsonlLimitError as error:
            if str(error).startswith("F3_PRIVATE_OBSERVATION_FILE"):
                raise LiveMemoryError("F3_LIVE_MEMORY_PRIVATE_FILE_REQUIRED") from error
            raise LiveMemoryLimitError(str(error)) from error

    def _record(self, row):
        """旁证失败只永久失效其健康，不改原SDK返回或异常。"""
        with self._lock:
            try:
                self._append(row)
                self._count += 1
            except Exception:
                self._errors += 1

    def _request_summary(self, args, kwargs):
        """所有断言依据最终SDK实参，缺实际渲染配对时不补候选为已消费。"""
        from app.llm.client import _request_tokens

        messages = kwargs.get("messages", [])
        texts = list(_strings(messages))
        with self._lock:
            capture = self._captures.pop(self._key(messages), None)
        selected, revoked, audit = capture if capture is not None else ((), (), {})
        text = "\n".join(texts)
        audit_keys = ("policy_version", "estimator_version", "candidate_digest", "budget", "estimated_cost",
                      "nonmemory_cost", "input_limit", "omissions", "projection_revision", "source_input_id",
                      "revoked_count", "context_capability", "policy_settings", "projection_id",
                      "parent_projection_id", "output_reserve")
        return {**sdk_identity(), "sdk_request_sha256": digest({"args": args, "request": kwargs}),
            "memory_binding_valid": capture is not None,
            "selected": [{"id": item.id, "hash": hashlib.sha256(item.content.encode()).hexdigest(),
                "complete_in_sdk": any(item.content in value for value in texts),
                "occurrences": sum(value.count(item.content) for value in texts)} for item in selected],
            "request_estimate": _request_tokens(messages),
            "sidecar_absent": all(marker not in text for marker in
                                  ("candidate_digest", "policy_version", "generations")),
            "revoked_fact_markers_absent": all(item not in value for item in revoked for value in texts),
            "projection_audit": {key: audit[key] for key in audit_keys if key in audit},
            "real_vendor_qualified": False, "real_model_quality_passed": False}

    def observe(self, client):
        """原create/with_options结果、异常、流和请求实参完全透传，不添加重试。"""
        original = client.chat.completions.create
        if getattr(original, "_f3_memory_observed", False):
            return client
        options = getattr(client, "with_options", None)

        @wraps(original)
        def create(*args, **kwargs):
            """实际调用后记录原usage；失败保留原异常对象及安全失败摘要。"""
            try:
                row = self._request_summary(args, kwargs)
            except Exception:
                with self._lock:
                    self._errors += 1
                row = None
            try:
                result = original(*args, **kwargs)
            except BaseException as error:
                if row is not None:
                    row.update(status="error", actual_provider_call=False,
                               error_class_sha256=digest(type(error).__name__), usage=None)
                    self._record(row)
                raise
            if row is not None:
                usage = getattr(result, "usage", None)
                row.update(status="stream_opened" if kwargs.get("stream") else "completed",
                    actual_provider_call=True, usage={key: getattr(usage, key, None)
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens")})
                self._record(row)
            return result

        create._f3_memory_observed = True
        client.chat.completions.create = create
        if callable(options):
            def with_options(*args, **kwargs):
                """原超时副本继续安全观察，原选项及SDK副本由原方法决定。"""
                return self.observe(options(*args, **kwargs))

            client.with_options = with_options
        return client

    def health(self) -> dict:
        """只报告旁证计数与粘性健康，不能给真实供应商或模型质量验收资格。"""
        with self._lock:
            return {"receipt_count": self._count, "error_count": self._errors,
                    "valid": self._errors == 0, "real_vendor_qualified": False,
                    "real_model_quality_passed": False}


@contextmanager
def memory_dispatch_hook(module, spec: dict, observer: MemoryWireObserver):
    """原最终选择前只收紧两个精确control，退出恢复原符号，不传播生产ContextVar。"""
    from app.memory.projection import MemoryProjectionPolicy
    from app.memory.request_projection import MemoryRequestPayload

    quality = validate_memory_quality(spec)
    _private_runtime(observer.runtime)
    if (os.environ.get("FULLSTACK_FINAL_F3_LIVE") != "1"
            or os.environ.get("F3_LIVE_RUNTIME") != str(observer.runtime)):
        raise LiveMemoryError("F3_LIVE_MEMORY_ISOLATION_REQUIRED")
    controls = {(spec["tenant"]["id"], spec["owner"]["id"], quality["agents"][branch]["control"]["id"])
                for branch in CASES}
    original = module._memory_request_messages

    @wraps(original)
    def render(client, system_prompt, payload, output_tokens):
        """源选择费用仍由原renderer计算，不直接删候选、槽位、管理记录或模型输出。"""
        if isinstance(payload, MemoryRequestPayload):
            envelope = payload.envelope
            scopes = {item.scope for item in envelope.candidates}
            if scopes & controls:
                if len(scopes) != 1 or not envelope.source_input_id:
                    raise LiveMemoryError("F3_LIVE_MEMORY_CONTROL_IDENTITY_INVALID")
                envelope.policy = MemoryProjectionPolicy(configured_memory_limit=0,
                    max_items=envelope.policy.max_items, max_fact_characters=envelope.policy.max_fact_characters)
        messages = original(client, system_prompt, payload, output_tokens)
        try:
            observer.capture(payload, messages)
        except Exception:
            with observer._lock:
                observer._errors += 1
        return messages

    module._memory_request_messages = render
    try:
        yield
    finally:
        if module._memory_request_messages is render:
            module._memory_request_messages = original
