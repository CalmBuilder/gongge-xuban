"""
@Time       : 2026/10/13
@Author     : zhanglp8181
@File       : final_f3_live_fixture.py
@CallChain  : 自有live启动 → 原资源发布 → 私有只读事实 → 真实页面两分支
@Description: 种合成资源与人工标签并限制私有JSON读写，不种成功、不安装模型替身。
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from final_f3_preparation import digest

BUSINESS_FIELDS = ("namespace", "object_id", "product_name", "quantity", "customer", "due_date")
MAX_PRIVATE_JSON_BYTES = 2 * 1024 * 1024
MAX_PRIVATE_JSON_NODES = 50_000
MAX_PRIVATE_JSON_DEPTH = 64


class LiveFixtureError(ValueError):
    """固定准备错误，不携带私有配置、凭据、端点或业务正文。"""


class LiveFixtureLimitError(LiveFixtureError):
    """私有JSON文件、响应或投影超过固定字节和结构预算。"""


@dataclass(frozen=True)
class LiveSpec:
    """私有fixture配置不进入repr，结构通过不代表模型或供应商资格。"""

    value: dict = field(repr=False)

    def safe_summary(self) -> dict:
        """对外仅提供摘要和未验标记，不返回真实模型或合成口令。"""
        return {"spec_sha256": digest(self.value), "provider_kind": "simulated_async_service",
                "real_vendor_qualified": False, "JRN08_passed": False}


def _required_text(value: object) -> bool:
    """只接受非空明确字符串，防止布尔或缺项被隐式转换。"""
    return isinstance(value, str) and bool(value.strip())


def validate_spec(value: object, *, runtime: Path) -> LiveSpec:
    """纯合同验证显式双库、I1预注册知识标签、两个账号及独立模拟来源。"""
    try:
        if not isinstance(value, dict):
            raise ValueError
        required = {"schema_version", "run_id", "runtime_name", "database_dialect", "tenant",
                    "owner", "reviewer", "agents", "model", "provider", "control_key", "skill",
                    "tool", "knowledge", "memories", "date_labels", "cases"}
        if (not required <= value.keys() or value.keys() - required - {"memory_quality"}
                or value["schema_version"] != "final-f3-live-simulated-fixture-v1"):
            raise ValueError
        if value["runtime_name"] != runtime.name or value["database_dialect"] not in {"sqlite", "mysql"}:
            raise ValueError
        if not re.fullmatch(r"f3-[a-z0-9-]+", value["run_id"]):
            raise ValueError
        for section, keys in (("tenant", ("id", "name")), ("skill", ("id", "name", "version")),
                              ("tool", ("id", "name", "display_name"))):
            if not all(_required_text(value[section][key]) for key in keys):
                raise ValueError
        for role in ("owner", "reviewer"):
            if not all(_required_text(value[role][key]) for key in
                       ("id", "username", "password", "display_name")) or value[role]["role"] != "admin":
                raise ValueError
        if any(value["owner"][key] == value["reviewer"][key] for key in ("id", "username")):
            raise ValueError
        if set(value["agents"]) != {"SOP", "DTA"} or not all(
            _required_text(value["agents"][kind][key]) for kind in ("SOP", "DTA") for key in ("id", "name")
        ) or value["agents"]["SOP"]["id"] == value["agents"]["DTA"]["id"]:
            raise ValueError
        model = value["model"]
        if set(model) - {"id", "provider", "base_url", "model", "api_key", "temperature",
                         "max_output_tokens", "extra_body", "timeout_seconds"}:
            raise ValueError
        if not all(_required_text(model[key]) for key in ("id", "base_url", "model", "api_key")):
            raise ValueError
        url = urlsplit(model["base_url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError
        if url.query or url.fragment or url.hostname.lower() == "localhost":
            raise ValueError
        try:
            address = ipaddress.ip_address(url.hostname)
        except ValueError:
            address = None
        if address is not None and address.is_loopback:
            raise ValueError
        if model["provider"] != "openai_compatible" or type(model["temperature"]) not in (int, float):
            raise ValueError
        if model["temperature"] != 0 or type(model["max_output_tokens"]) is not int:
            raise ValueError
        if model["max_output_tokens"] < 1 or not isinstance(model["extra_body"], dict):
            raise ValueError
        provider = value["provider"]
        origin = urlsplit(provider["origin"])
        if (provider["provider_kind"] != "simulated_async_service" or origin.scheme != "http"
                or origin.hostname != "127.0.0.1" or not origin.port or origin.path
                or origin.query or origin.fragment or origin.username or origin.password):
            raise ValueError
        if not re.fullmatch(r"f3-sim-[0-9a-f]{16}", provider["namespace"]):
            raise ValueError
        if not _required_text(provider["key"]) or not _required_text(value["control_key"]):
            raise ValueError
        identities = set()
        if set(value["cases"]) != {"SOP", "DTA"}:
            raise ValueError
        for branch in value["cases"].values():
            if set(branch) != {"normal", "cancel", "safe_decline", "unknown"}:
                raise ValueError
            for case_name, case in branch.items():
                if not all(_required_text(case[key]) for key in ("message", "confirmation_message")):
                    raise ValueError
                business = case["expected_business"]
                if set(business) != set(BUSINESS_FIELDS) or business["namespace"] != provider["namespace"]:
                    raise ValueError
                identity = business["object_id"]
                if not re.fullmatch(r"f3-obj-[0-9a-f]{32}", identity) or identity in identities:
                    raise ValueError
                identities.add(identity)
                if type(business["quantity"]) is not int or not 1 <= business["quantity"] <= 10:
                    raise ValueError
                if business["customer"] != "SYNTHETIC-F3" or business["product_name"] != (
                    "SIMULATED_DECLINE" if case_name == "safe_decline" else "ALPHA91"
                ):
                    raise ValueError
                date.fromisoformat(business["due_date"])
        knowledge = value["knowledge"]
        if not all(_required_text(knowledge[key]) for key in
                   ("kb_name", "title", "filename", "old_content_md", "new_content_md",
                    "old_deadline", "new_deadline", "old_query", "new_query", "unknown_query", "guidance_slug")):
            raise ValueError
        if ("ZIRCON999UNKNOWN" not in knowledge["unknown_query"]
                or "CYPRESS42" in knowledge["unknown_query"]):
            raise ValueError
        if any(type(knowledge[key]) is not int for key in ("old_amount", "new_amount")):
            raise ValueError
        memories = value["memories"]
        if not isinstance(memories, list) or not memories or len({row["key"] for row in memories}) != len(memories):
            raise ValueError
        if not all(_required_text(row[key]) for row in memories for key in ("key", "kind", "content")):
            raise ValueError
        ZoneInfo(value["date_labels"]["timezone"])
        if datetime.fromisoformat(value["date_labels"]["anchor_utc"]).tzinfo is None:
            raise ValueError
        date.fromisoformat(value["date_labels"]["expected_due_date"])
        if "memory_quality" in value:
            from final_f3_live_memory import validate_memory_quality

            validate_memory_quality(value)
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise LiveFixtureError("F3_LIVE_SPEC_INVALID") from error
    return LiveSpec(deepcopy(value))


def _private_json_size(value: object) -> int:
    """在完整JSON编码前限制私有投影的精确UTF-8字节、节点和嵌套深度。"""
    size = 0
    nodes = 0
    ancestors: set[int] = set()

    def add(amount: int) -> None:
        """累计紧凑JSON字节并在超预算时立即拒绝。"""
        nonlocal size
        size += amount
        if size > MAX_PRIVATE_JSON_BYTES:
            raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_LIMIT")

    def string_size(text: str) -> int:
        """不构造整字符串副本，按JSON转义和UTF-8编码逐字符计量。"""
        result = 2
        for character in text:
            codepoint = ord(character)
            if character in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
                result += 2
            elif codepoint < 0x20:
                result += 6
            else:
                try:
                    result += len(character.encode("utf-8"))
                except UnicodeEncodeError as error:
                    raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_INVALID") from error
            if result > MAX_PRIVATE_JSON_BYTES:
                raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_LIMIT")
        return result

    def measure(item: object, depth: int) -> None:
        """只接收JSON原生值，并拒绝循环、过深或过大的结构。"""
        nonlocal nodes
        nodes += 1
        if nodes > MAX_PRIVATE_JSON_NODES or depth > MAX_PRIVATE_JSON_DEPTH:
            raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_LIMIT")
        if item is None:
            add(4)
        elif item is True:
            add(4)
        elif item is False:
            add(5)
        elif isinstance(item, str):
            add(string_size(item))
        elif isinstance(item, int):
            if item.bit_length() > 256:
                raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_LIMIT")
            add(len(str(item)))
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_INVALID")
            add(len(repr(item)))
        elif isinstance(item, dict):
            identity = id(item)
            if identity in ancestors:
                raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_INVALID")
            ancestors.add(identity)
            add(2)
            for index, (key, child) in enumerate(item.items()):
                if not isinstance(key, str):
                    raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_INVALID")
                if index:
                    add(1)
                add(string_size(key) + 1)
                measure(child, depth + 1)
            ancestors.remove(identity)
        elif isinstance(item, list):
            identity = id(item)
            if identity in ancestors:
                raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_INVALID")
            ancestors.add(identity)
            add(2)
            for index, child in enumerate(item):
                if index:
                    add(1)
                measure(child, depth + 1)
            ancestors.remove(identity)
        else:
            raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_INVALID")

    measure(value, 0)
    return size


def _private_json_bytes(value: object) -> bytes:
    """只在结构预检通过后编码私有JSON，并再次核对编码字节上限。"""
    _private_json_size(value)
    body = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    try:
        for chunk in encoder.iterencode(value):
            encoded = chunk.encode("utf-8")
            if len(body) + len(encoded) > MAX_PRIVATE_JSON_BYTES:
                raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_LIMIT")
            body.extend(encoded)
    except (TypeError, ValueError) as error:
        if isinstance(error, LiveFixtureError):
            raise
        raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_INVALID") from error
    return bytes(body)


def _preflight_private_json(data: bytes) -> None:
    """在JSON解析前限制私有文件的结构节点与嵌套深度。"""
    nodes = 0
    depth = 0
    in_string = False
    escaped = False
    in_primitive = False
    for value in data:
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
            if depth > MAX_PRIVATE_JSON_DEPTH:
                raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_LIMIT")
        elif value in (0x7D, 0x5D):
            nodes += 1
            depth -= 1
            in_primitive = False
            if depth < 0:
                raise LiveFixtureError("F3_LIVE_PRIVATE_SPEC_REQUIRED")
        elif value in (0x2C, 0x3A):
            nodes += 1
            in_primitive = False
        elif not in_primitive:
            nodes += 1
            in_primitive = True
        if nodes > MAX_PRIVATE_JSON_NODES:
            raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_LIMIT")
    if in_string or escaped or depth != 0:
        raise LiveFixtureError("F3_LIVE_PRIVATE_SPEC_REQUIRED")


def _private_file_read(path: Path, runtime: Path) -> tuple[dict, bytes]:
    """限字节和JSON复杂度后读取当前UID自有的私有常规文件。"""
    def same_file_version(current: os.stat_result, expected: os.stat_result) -> bool:
        """比较文件身份和修改版本但忽略读取产生的atime变化。"""
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        return all(getattr(current, field) == getattr(expected, field) for field in fields)

    try:
        info = path.lstat()
        if (path.parent != runtime or path.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError
        if info.st_size > MAX_PRIVATE_JSON_BYTES:
            raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_LIMIT")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            before_read = os.fstat(stream.fileno())
            if not same_file_version(before_read, info) or before_read.st_size > MAX_PRIVATE_JSON_BYTES:
                raise ValueError
            data = stream.read(MAX_PRIVATE_JSON_BYTES + 1)
            if len(data) > MAX_PRIVATE_JSON_BYTES or not same_file_version(os.fstat(stream.fileno()), info):
                raise LiveFixtureLimitError("F3_LIVE_PRIVATE_JSON_LIMIT")
        _preflight_private_json(data)
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        _private_json_size(value)
        return value, data
    except LiveFixtureLimitError:
        raise
    except (OSError, ValueError) as error:
        raise LiveFixtureError("F3_LIVE_PRIVATE_SPEC_REQUIRED") from error


def _private_read(path: Path, runtime: Path) -> dict:
    """读取经字节、复杂度及所有权预检的私有JSON对象。"""
    return _private_file_read(path, runtime)[0]


def load_spec(runtime: Path) -> LiveSpec:
    """只加载当前UID自有私有spec，不自动读取根或后端dotenv。"""
    path = Path(os.environ.get("F3_LIVE_FIXTURE_SPEC_PATH", ""))
    return validate_spec(_private_read(path, runtime), runtime=runtime)


def _assert_isolated(runtime: Path) -> LiveSpec:
    """写入前核显式开启、自有运行根及原启动标记，拒绝共享业务库。"""
    if (os.environ.get("FULLSTACK_FINAL_F3_LIVE") != "1"
            or os.environ.get("F3_LIVE_RUNTIME") != str(runtime)
            or not runtime.name.startswith("f3-live-") or runtime.is_symlink()
            or stat.S_IMODE(runtime.stat().st_mode) != 0o700):
        raise LiveFixtureError("F3_LIVE_EXPLICIT_ISOLATION_REQUIRED")
    spec = load_spec(runtime)
    marker = _private_read(runtime / ".f3-live-owned.private.json", runtime)
    if marker.get("uid") != os.getuid() or marker.get("run_id") != spec.value["run_id"]:
        raise LiveFixtureError("F3_LIVE_EXPLICIT_ISOLATION_REQUIRED")
    return spec


def _check_database(db, spec: LiveSpec) -> None:
    """仅允许当前私有运行根SQLite或原启动器随机原生MySQL库。"""
    from sqlalchemy.engine import make_url

    selected = make_url(os.environ.get("DATABASE_URL", ""))
    actual = db.get_bind().url
    if actual != selected:
        raise LiveFixtureError("F3_LIVE_EXPLICIT_ISOLATION_REQUIRED")
    if spec.value["database_dialect"] == "sqlite":
        if actual.database != str(Path(os.environ["F3_LIVE_RUNTIME"]) / "journey.sqlite3"):
            raise LiveFixtureError("F3_LIVE_EXPLICIT_ISOLATION_REQUIRED")
    elif (actual.get_backend_name() != "mysql" or actual.port in (None, 3306)
          or not (actual.database or "").startswith("gongge_xuban_test_")
          or not (actual.username or "").startswith("st_test_")):
        raise LiveFixtureError("F3_LIVE_EXPLICIT_ISOLATION_REQUIRED")


def seed_live_resources(db, spec: LiveSpec) -> dict:
    """原发布链准备合成资源，模型保持unverified，所有运行账本保持空。"""
    from sqlmodel import select
    from app.agents.branching import (
        _ensure_binding, ensure_open_gallery_binding, sync_knowledge_branch_from_overall,
    )
    from app.api.knowledge_bases import create_knowledge_base, update_knowledge_governance
    from app.api.skills import create_skill
    from app.db.models import (
        AgentProfile, AgentUsage, KnowledgeBase, MemoryRecord, ModelConfig, SkillVersion, Tenant,
        Tool, User,
    )
    from app.dynamic_tasks.capability_catalog import ToolReliabilityContract, publish_tool_contract
    from app.knowledge.revisions import INITIAL_REVISION_DOCUMENT_ID, run_compatibility_revision
    from app.knowledge.schema import KnowledgeBaseCreateRequest, KnowledgeBaseGovernanceUpdateRequest
    from app.security.auth import hash_password
    from app.security.encryption import encrypt_secret
    from app.skills.skill_schema import SkillCard, SkillCreateRequest

    value = spec.value
    _check_database(db, spec)
    tenant = value["tenant"]["id"]
    if db.get(Tenant, tenant) is not None:
        raise LiveFixtureError("F3_LIVE_SEED_ALREADY_EXISTS")
    db.add(Tenant(**value["tenant"]))
    for role in ("owner", "reviewer"):
        account = value[role]
        db.add(User(id=account["id"], tenant_id=tenant, username=account["username"],
                    display_name=account["display_name"], role="admin",
                    password_hash=hash_password(account["password"])))
    db.add(AgentProfile(id=tenant + "_overall", tenant_id=tenant, name="F3合成整体资源池",
                        is_overall=True, owner_user_id=value["owner"]["id"], status="active"))
    for agent in value["agents"].values():
        db.add(AgentProfile(**agent, tenant_id=tenant, status="active",
                            owner_user_id=value["owner"]["id"],
                            metadata_json={"owner_user_id": value["owner"]["id"]}))
        for role in ("owner", "reviewer"):
            db.add(AgentUsage(tenant_id=tenant, user_id=value[role]["id"], agent_id=agent["id"]))
    model = value["model"]
    db.add(ModelConfig(id=model["id"], tenant_id=tenant, name="F3真实模型",
                       provider=model["provider"], base_url=model["base_url"], model=model["model"],
                       api_key_encrypted=encrypt_secret(model["api_key"]), temperature=model["temperature"],
                       max_output_tokens=model["max_output_tokens"], extra_body_json=model["extra_body"],
                       is_default=True, enabled=True))
    db.commit()
    owner = db.get(User, value["owner"]["id"])
    knowledge = value["knowledge"]
    kb_read = create_knowledge_base(
        KnowledgeBaseCreateRequest(tenant_id=tenant, name=knowledge["kb_name"]),
        agent_id=None, db=db, current_user=owner,
    )
    kb = db.get(KnowledgeBase, kb_read.id)
    update_knowledge_governance(kb.id, KnowledgeBaseGovernanceUpdateRequest(
        tenant_id=tenant, expected_revision=kb.revision, access_scope="tenant",
        download_policy="restricted"), db=db, current_user=owner)
    source = knowledge["old_content_md"]
    job = run_compatibility_revision(db, tenant_id=tenant, knowledge_base_id=kb.id,
        scope="overall", actor_user_id=owner.id, document_id=INITIAL_REVISION_DOCUMENT_ID,
        operation_kind="upload", mutation_payload={"filename": knowledge["filename"],
            "title": knowledge["title"], "content_md": source,
            "content_base64": base64.b64encode(source.encode()).decode()})
    if job.status != "completed":
        raise LiveFixtureError("F3_LIVE_INITIAL_PUBLICATION_FAILED")
    # SOP质量正文必须拥有独立的整体知识库，避免DTA正文编辑先改写同一文档后
    # 让SOP旧版本基线失真；两边仍都通过原创建、修订、分支同步链路发布。
    sop_knowledge_name = knowledge["kb_name"] + "（SOP质量隔离）"
    sop_kb_read = create_knowledge_base(
        KnowledgeBaseCreateRequest(tenant_id=tenant, name=sop_knowledge_name),
        agent_id=None, db=db, current_user=owner,
    )
    sop_kb = db.get(KnowledgeBase, sop_kb_read.id)
    update_knowledge_governance(sop_kb.id, KnowledgeBaseGovernanceUpdateRequest(
        tenant_id=tenant, expected_revision=sop_kb.revision, access_scope="tenant",
        download_policy="restricted"), db=db, current_user=owner)
    sop_job = run_compatibility_revision(
        db, tenant_id=tenant, knowledge_base_id=sop_kb.id, scope="overall",
        actor_user_id=owner.id, document_id=INITIAL_REVISION_DOCUMENT_ID,
        mutation_payload={"filename": knowledge["filename"], "title": knowledge["title"],
                          "content_md": source,
                          "content_base64": base64.b64encode(source.encode()).decode()},
        operation_kind="upload",
    )
    if sop_job.status != "completed":
        raise LiveFixtureError("F3_LIVE_SOP_INITIAL_PUBLICATION_FAILED")
    properties = {
        key: {"type": "integer" if key == "quantity" else "string"}
        for key in BUSINESS_FIELDS
    }
    properties["due_date"] = {
        "type": "string",
        "format": "date",
        "description": (
            "公历日期，必须是 YYYY-MM-DD。用户使用相对日期时，按当前 execution_context.time_context "
            "中的 local_at 和 timezone 换算；不得改用当前系统时钟或原样输出相对词。"
        ),
    }
    output_properties = {**properties, "receipt_id": {"type": "string"}}
    output_schema = {"type": "object", "properties": output_properties,
                     "required": list(BUSINESS_FIELDS), "additionalProperties": False}
    provider = value["provider"]
    contract = ToolReliabilityContract.model_validate({
        "risk_class": "external_write", "side_effect": "external", "confirmation_policy": "once",
        "timeout_policy": "unknown", "idempotency": {"mode": "request_key",
        "remote_scope": provider["namespace"]}, "reconcile": {"supported": True,
        "tool_name": value["tool"]["name"], "reference_source": "output.receipt_id",
        "terminal_status_mapping": {"completed": "complete"}}, "dynamic_task_enabled": True,
        "execution_mode": "detached", "async_strategy": "provider_task",
        "status_url": provider["origin"] + "/simulation/tasks/{taskId}",
        "status_mapping": {"accepted": "working", "working": "working", "completed": "complete",
                           "cancelled": "cancelled", "failed": "failed"},
        "poll_interval_seconds": 1, "max_tracking_seconds": 300,
        "model_visibility": {"allowed_paths": ["input." + key for key in properties]
                             + ["output." + key for key in output_properties],
                             "user_display_paths": ["output." + key for key in output_properties],
                             "audit_only_paths": []},
    })
    tool = Tool(**value["tool"], tenant_id=tenant, method="POST",
                description="独立模拟服务中的合成异步订单，等待实际回执；不是实际供应商。",
                url=provider["origin"] + "/simulation/tasks",
                headers_json={"X-Simulation-Key": provider["key"]},
                input_schema={"type": "object", "properties": properties,
                              "required": list(properties), "additionalProperties": False},
                output_schema=output_schema, enabled=True)
    publish_tool_contract(tool, contract)
    db.add(tool)
    db.flush()
    ensure_open_gallery_binding(db, tenant, "tool", tool.id, "active")
    memory_ids = []
    for kind, agent in value["agents"].items():
        agent_kb = sop_kb if kind == "SOP" else kb
        _ensure_binding(db, tenant, agent["id"], "knowledge_base", agent_kb.id, "active")
        sync_knowledge_branch_from_overall(db, tenant, agent["id"], agent_kb.id)
        _ensure_binding(db, tenant, agent["id"], "tool", tool.id, "active")
        for memory in value["memories"]:
            row = MemoryRecord(tenant_id=tenant, agent_id=agent["id"], agent_scope_key=agent["id"],
                user_id=owner.id, username=owner.username, kind=memory["kind"],
                content=memory["content"], importance=0.8, metadata_json={"key": memory["key"]})
            db.add(row)
            db.flush()
            memory_ids.append(row.id)
    db.commit()
    confirmation = value["cases"]["SOP"]["normal"]["confirmation_message"]
    nodes = [
        {"node_id": "policy", "type": "knowledge_query", "name": knowledge["old_query"],
         "instruction": knowledge["old_query"], "allowed_actions": ["knowledge_query"],
         "metadata": {"operation_result_key": "policy", "knowledge_query": {
             "query_type": "policy_check", "desired_evidence": knowledge["title"] + "额度和截止"}}},
        {"node_id": "collect", "type": "collect_info", "name": "补齐合成订单六项",
         "expected_user_info": list(BUSINESS_FIELDS), "allowed_actions": ["ask_user", "continue_flow"]},
        {"node_id": "confirm", "type": "collect_info", "name": "本人明确确认",
         "expected_user_info": ["confirmed"], "allowed_actions": ["ask_user", "continue_flow"],
         "metadata": {"confirmation_policy": {"slot_name": "confirmed",
                      "phrase_values": {confirmation: "yes"}, "prompt": "请回复" + confirmation}}},
        {"node_id": "submit", "type": "tool_call", "name": "异步合成订单提交",
         "allowed_actions": ["call_tool:" + tool.name], "metadata": {
             "operation_result_key": "delivery",
             "operation_input": {key: "slots." + key for key in BUSINESS_FIELDS}}},
        {"node_id": "done", "type": "response", "name": "等待实际回执后准确回答",
         "instruction": "仅用原工具结果与知识证据回答，保留准确回执及引用。",
         "allowed_actions": ["answer_user"]},
    ]
    card = SkillCard.model_validate({"skill_id": value["skill"]["id"], "name": value["skill"]["name"],
        "version": value["skill"]["version"], "execution_mode": "deterministic",
        "description": "合成订单办理，原知识、确认、异步工具与回执闭环。",
        "trigger_intents": ["提交F3异步合成订单", "F3合成订单"],
        "goal": ["合法确认后异步办理合成订单，准确返回远端回执"],
        "condition_schemas": {"slots": {"type": "object", "properties": {
            **properties, "confirmed": {"type": "string"}}},
            "tool_result": {"type": "object", "properties": {"delivery": {"type": "object"}}}},
        "nodes": nodes, "edges": [{"source_node_id": a["node_id"], "next_node_id": b["node_id"]}
            for a, b in zip(nodes, nodes[1:])], "start_node_id": "policy", "terminal_node_ids": ["done"]})
    created = create_skill(SkillCreateRequest(tenant_id=tenant, content=card, status="published"),
                          agent_id=value["agents"]["SOP"]["id"], db=db, current_user=owner)
    skill_version = db.exec(select(SkillVersion).where(SkillVersion.tenant_id == tenant,
        SkillVersion.skill_id == created.skill_id)).first()

    def create_knowledge_sop_card(label: str, query: str, trigger: str) -> None:
        """为本轮模型质量分支建立只读SOP知识技能，固定查询与唯一触发词。"""
        knowledge_nodes = [
            {"node_id": "policy", "type": "knowledge_query", "name": query,
             "instruction": query, "allowed_actions": ["knowledge_query"],
             "metadata": {"operation_result_key": "policy", "knowledge_query": {
                 "query_type": "policy_check", "desired_evidence": knowledge["title"]
                 + "额度和截止"}}},
            {"node_id": "done", "type": "response", "name": "依据知识证据回答",
             "instruction": "只依据本轮knowledge.search证据回答，证据不足时明确无答案并保留引用。",
             "allowed_actions": ["answer_user"]},
        ]
        knowledge_card = SkillCard.model_validate({
            "skill_id": f"{value['skill']['id']}-{label}",
            "name": f"F3 SOP知识质量-{label}", "version": "1.0.0",
            "execution_mode": "deterministic",
            "description": "F3模型质量只读知识检索，固定版本、来源和无答案语义。",
            "trigger_intents": [trigger],
            "goal": ["按冻结知识证据回答或明确无答案"],
            "condition_schemas": {"slots": {"type": "object", "properties": {}}},
            "nodes": knowledge_nodes,
            "edges": [{"source_node_id": "policy", "next_node_id": "done"}],
            "start_node_id": "policy", "terminal_node_ids": ["done"],
        })
        create_skill(SkillCreateRequest(tenant_id=tenant, content=knowledge_card,
                    status="published"), agent_id=value["agents"]["SOP"]["id"],
                    db=db, current_user=owner)

    create_knowledge_sop_card("knowledge-old", value["knowledge"]["old_query"],
                              "F3 SOP知识质量旧版本")
    create_knowledge_sop_card("knowledge-new", value["knowledge"]["new_query"],
                              "F3 SOP知识质量新版本")
    create_knowledge_sop_card("knowledge-unknown", value["knowledge"]["unknown_query"],
                              "F3 SOP知识质量未知版本")
    db.commit()
    result = {"provider_kind": "simulated_async_service", "real_vendor_qualified": False,
              "run_id": value["run_id"], "spec_sha256": digest(value),
              "skill_id": created.skill_id, "skill_version_id": skill_version.id,
              "model": {"id": model["id"], "name": "F3真实模型"},
              "provider": {"namespace": provider["namespace"], "provider_kind": "simulated_async_service"},
              "agents": {kind: {**agent, "tenant_id": tenant,
                                "gallery_label": agent["name"] + " @" + owner.id}
                         for kind, agent in value["agents"].items()},
              "cases": deepcopy(value["cases"]), "memories": deepcopy(value["memories"]),
              "memory_ids": memory_ids, "date_labels": deepcopy(value["date_labels"]),
              "knowledge": {**knowledge, "kb_id": kb.id, "old_version_id": job.published_version_id,
                            "old_document_id": job.published_document_id, "new_version_id": None},
              "sop_knowledge": {**knowledge, "kb_name": sop_knowledge_name, "kb_id": sop_kb.id,
                                "old_version_id": sop_job.published_version_id,
                                "old_document_id": sop_job.published_document_id, "new_version_id": None}}
    for role in ("owner", "reviewer"):
        account = value[role]
        result[role] = {"credentials": {key: account[key] for key in ("username", "password")},
            "expected": {key: account[key] for key in ("id", "username", "role")}
            | {"tenant_id": tenant, "membership_status": "active"}}
    if "memory_quality" in value:
        from final_f3_live_memory import seed

        result["memory_quality"] = seed(db, value)
    return result


def prepare_live(runtime: Path, *, reuse: bool) -> dict:
    """显式自有DB才初始化，真重启只读核原身份、不重种运行状态。"""
    spec = _assert_isolated(runtime)
    from sqlmodel import Session
    from app.db import engine
    from app.db.models import Tenant

    with Session(engine) as db:
        _check_database(db, spec)
        if reuse:
            value = _private_read(runtime / "initial.private.json", runtime)
            if value.get("spec_sha256") != digest(spec.value) or db.get(Tenant, spec.value["tenant"]["id"]) is None:
                raise LiveFixtureError("F3_LIVE_REUSE_IDENTITY_MISMATCH")
            return value
        value = seed_live_resources(db, spec)
    path = runtime / "initial.private.json"
    body = _private_json_bytes(value)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if stream.write(body) != len(body):
                raise OSError("F3_LIVE_PRIVATE_JSON_WRITE_FAILED")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return value


def _walk(value: object):
    """遍历原JSON对象供白名单证据读回，不生成缺失业务结果。"""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _knowledge_result_summary(result_json: object) -> dict:
    """从原知识Operation根结果保留结果等级的召回与完整性状态。"""
    result = next((item for item in _walk(result_json)
                   if item.get("outcome") in {"evidence_found", "insufficient", "no_match"}
                   and isinstance(item.get("evidence_sufficiency"), dict)), {})
    sufficiency = result.get("evidence_sufficiency")
    return {
        "outcome": result.get("outcome"),
        "evidence_required": isinstance(sufficiency, dict) and sufficiency.get("required") is True,
        "evidence_satisfied": isinstance(sufficiency, dict) and sufficiency.get("satisfied") is True,
    }


def _fields(row, keys: tuple | list) -> dict:
    """只展开明确指定的原持久列，避免模型或数据库凭据误归档。"""
    return {key: item.isoformat() if isinstance(item := getattr(row, key, None), datetime) else item
            for key in keys}


def _facts(text: str, spec: LiveSpec) -> dict:
    """用人工金额及截止标签匹配实际文本，不调用模型自评分数。"""
    knowledge = spec.value["knowledge"]
    value = {key: bool(re.search(r"(?<!\d)" + re.escape(str(knowledge[key])) + r"(?!\d)", text))
             for key in ("old_amount", "new_amount", "old_deadline", "new_deadline")}
    value["explicit_no_answer"] = bool(re.search(r"未找到|没有.*(?:资料|证据)|无.*(?:资料|证据|答案)|无法.*(?:确认|回答)|缺乏|未检索到", text))
    value["policy_number_present"] = bool(re.search(r"\d+\s*元|\d{1,2}\s*[:：]\s*\d{2}", text))
    return value


def _business_outcome(text: str) -> dict[str, bool]:
    """只暴露拒绝说明与肯定成功宣称两个布尔值，避免归档最终回复正文。"""
    failure_notice = bool(re.search(
        r"失败|拒绝|驳回|无法|未能|错误|HTTP\s*[45]\d\d|\b(?:failed|rejected|declined|error)\b",
        text, flags=re.IGNORECASE,
    ))
    success_claim = bool(re.search(
        r"(?:已经?|已)?成功(?:提交|创建|办理|下单|处理|完成)|"
        r"(?:提交|创建|办理|下单|处理)(?:已经?|已)?成功|"
        r"(?:order|task|request)\s+(?:was\s+)?(?:submitted|created|completed)\s+successfully|"
        r"successfully\s+(?:submitted|created|completed)",
        text, flags=re.IGNORECASE,
    ))
    return {"failure_notice": failure_notice, "success_claim": success_claim}


def _business(value: object) -> dict:
    """仅读原注册业务六项和合成回执；自由字段保持摘要。"""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key in (*BUSINESS_FIELDS, "receipt_id")}


def approval_projection(db, spec: LiveSpec, attention_id: str) -> dict:
    """精确原DTA实例/Attention/Operation，未授权字段保持空且不伪造完成。"""
    from app.db.models import SopInstance, SopOperation, SopWorkItem

    value = spec.value
    attention = db.get(SopWorkItem, attention_id)
    instance = db.get(SopInstance, attention.instance_id) if attention else None
    if (attention is None or instance is None or attention.tenant_id != value["tenant"]["id"]
            or instance.tenant_id != attention.tenant_id or instance.kind != "dynamic_task"
            or instance.agent_id != value["agents"]["DTA"]["id"]
            or instance.initiator_user_id != value["owner"]["id"]):
        raise LiveFixtureError("F3_LIVE_APPROVAL_NOT_FOUND")
    payload = attention.payload_json or {}
    operation_id = payload.get("operation_id") or attention.source_ref
    operation = db.get(SopOperation, operation_id) if operation_id else None
    if operation is None or operation.instance_id != instance.id or operation.tenant_id != attention.tenant_id:
        raise LiveFixtureError("F3_LIVE_APPROVAL_NOT_FOUND")
    return {"instance": _fields(instance, ("id", "kind", "tenant_id", "agent_id", "initiator_user_id",
                "session_id", "current_plan_revision_id")),
            "attention": _fields(attention, ("id", "tenant_id", "status", "source_type", "source_ref",
                "revision", "title")) | {"kind": attention.attention_kind, "execution_id": instance.id,
                               "session_id": instance.session_id,
                               "allowed_commands": attention.allowed_commands_json,
                               "available_commands": attention.allowed_commands_json,
                               "payload": payload, "resolution": attention.resolution_json or {}},
            "operation": _fields(operation, ("id", "instance_id", "tenant_id", "operation_name",
                "status", "request_fingerprint", "capability_checksum", "approval_fingerprint",
                "approval_work_item_id", "approved_by_user_id", "approved_at", "request_json", "effect_kind"))}


def _persistent_sdk_bindings(hashes: dict, bindings: dict, *, tenant: str, owner: str,
                             instances: list, sessions: list, messages: list, plans: list,
                             signals: list, versions: list,
                             memory_jobs: list | tuple = ()) -> tuple[dict, bool]:
    """只解原SDK实际携带身份，并按同租户/本人/会话/实例核历史计划、冻结版本及Signal。"""
    result = {**bindings, "plan_id": None, "skill_version_id": None, "signal_id": None}
    session = next((row for row in sessions if row.id == result.get("session_id")), None)
    if session is None and "session_id" in hashes:
        result["session_id"] = None
    if session is not None and any(
        key in hashes and digest(value) != hashes[key]
        for key, value in (("tenant_id", session.tenant_id), ("user_id", session.user_id),
                           ("agent_id", session.agent_id))
    ):
        result["session_id"] = None
        session = None
    instance = next((row for row in instances if row.id == result.get("instance_id")), None)
    if instance is None and "instance_id" in hashes:
        result["instance_id"] = None
    if instance is not None and (instance.tenant_id != tenant
            or instance.initiator_user_id != owner or any(
                key in hashes and digest(value) != hashes[key]
                for key, value in (("tenant_id", instance.tenant_id),
                    ("user_id", instance.initiator_user_id), ("agent_id", instance.agent_id),
                    ("session_id", instance.session_id)))):
        result["instance_id"] = None
        instance = None
    for key in ("source_input_id", "turn_id"):
        source = next((row for row in messages if row.id == result.get(key)), None)
        if source is not None and (getattr(source, "tenant_id", None) != tenant
                or source.role != "user"
                or session is not None and getattr(source, "session_id", None) != session.id
                or instance is not None
                    and getattr(source, "session_id", None) != instance.session_id):
            result[key] = None
    if "task_id" in hashes:
        matching_jobs = [row for row in memory_jobs if row.id == result.get("task_id")]
        job = matching_jobs[0] if len(matching_jobs) == 1 else None
        source = next((row for row in messages if row.id == result.get("source_input_id")), None)
        assistant = None
        if job is not None and getattr(job, "source_assistant_message_id", None):
            matching_assistants = [row for row in messages
                                   if row.id == job.source_assistant_message_id]
            assistant = matching_assistants[0] if len(matching_assistants) == 1 else None
            assistant_metadata = getattr(assistant, "metadata_json", None) or {}
            assistant_turn_matches = bool(
                source is not None
                and all(str(assistant_metadata.get(key) or source.id) == source.id
                        for key in ("turn_id", "user_message_id"))
                and any(assistant_metadata.get(key) == source.id
                        for key in ("turn_id", "user_message_id"))
            )
            assistant_valid = bool(
                assistant is not None
                and assistant.tenant_id == tenant
                and assistant.session_id == (session.id if session else None)
                and assistant.role == "assistant"
                and assistant_turn_matches
            )
        else:
            assistant_valid = True
        if (job is None or source is None or session is None or job.tenant_id != tenant
                or job.user_id != owner or job.source_message_id != source.id
                or source.session_id != session.id
                or job.agent_id != session.agent_id
                or not assistant_valid
                or "agent_id" in hashes and digest(job.agent_id) != hashes["agent_id"]):
            result["task_id"] = None
    if instance is not None:
        result["plan_id"] = next((row.id for row in plans
            if instance.kind == "dynamic_task" and row.tenant_id == tenant
            and row.execution_id == instance.id and digest(row.id) == hashes.get("plan_id")), None)
        result["skill_version_id"] = next((row.id for row in versions
            if instance.kind == "sop" and row.id == instance.skill_version_id
            and row.tenant_id == tenant and row.skill_id == instance.skill_id
            and row.version == instance.skill_version
            and row.compiled_definition_checksum == instance.definition_checksum
            and digest(row.id) == hashes.get("skill_version_id")), None)
        result["signal_id"] = next((row.id for row in signals
            if row.tenant_id == tenant and row.execution_id == instance.id
            and digest(row.id) == hashes.get("signal_id")), None)
    valid = all(isinstance(result.get(key), str) and digest(result[key]) == value
                for key, value in hashes.items())
    if "source_input_id" in hashes and "turn_id" in hashes:
        valid = valid and result["source_input_id"] == result["turn_id"]
    return result, valid


def _expected_async_tool_facts(instance, plan, expected_name: str) -> dict:
    """只返回合成异步工具是否被冻结及计划引用计数，不输出能力或计划正文。"""
    def find_tool(snapshot_value: object) -> dict | None:
        """在原冻结目录中按唯一合成工具名查找能力白名单行。"""
        if not isinstance(snapshot_value, dict):
            return None
        tools = snapshot_value.get("tools")
        if not isinstance(tools, list):
            return None
        return next((item for item in tools if isinstance(item, dict)
                     and item.get("name") == expected_name), None)

    def contract_valid(tool: dict | None) -> bool:
        """确认原冻结模型视图和异步外写合同完整，但不暴露其配置字段。"""
        if tool is None:
            return False
        contract = tool.get("contract")
        model_view = tool.get("model_view")
        return (isinstance(contract, dict) and isinstance(model_view, dict)
                and model_view.get("name") == expected_name
                and contract.get("risk_class") == "external_write"
                and contract.get("dynamic_task_enabled") is True
                and contract.get("execution_mode") == "detached"
                and contract.get("async_strategy") == "provider_task")

    frozen_tool = find_tool(instance.capability_snapshot_json or {})
    plan_tool = find_tool(plan.capability_snapshot_json or {}) if plan is not None else None
    plan_json = plan.plan_json if plan is not None and isinstance(plan.plan_json, dict) else {}
    steps = plan_json.get("steps")
    safe_steps = steps if isinstance(steps, list) else []
    references = sum(
        1 for step in safe_steps
        if isinstance(step, dict) and isinstance(step.get("capability_refs"), list)
        and expected_name in step["capability_refs"]
    )
    return {
        "expected_async_tool_frozen": frozen_tool is not None,
        "expected_async_tool_frozen_for_plan": plan_tool is not None,
        "expected_async_tool_contract_valid": contract_valid(frozen_tool) and contract_valid(plan_tool),
        "current_plan_present": plan is not None,
        "current_plan_step_count": len(safe_steps) if plan is not None else None,
        "current_plan_references_expected_async_tool": references > 0,
    }


def _runtime_failure_code(instance) -> str | None:
    """只投影形如稳定标识符的执行错误代码，拒绝异常消息和模型正文。"""
    context = instance.context_json if isinstance(instance.context_json, dict) else {}
    terminal = instance.terminal_reason_json if isinstance(instance.terminal_reason_json, dict) else {}
    code = context.get("failure_code") or terminal.get("code")
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,127}", code) else None


def _safe_terminal_result_facts(result, instance) -> dict:
    """仅公开失败终态的枚举、稳定错误码和验证摘要，不返回结果正文。"""
    body = result.result_json if isinstance(result.result_json, dict) else {}
    context = body.get("context") if isinstance(body.get("context"), dict) else {}
    code = context.get("failure_code")
    safe_code = (code if isinstance(code, str)
                 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,127}", code) else None)
    projection_status = body.get("status")
    safe_status = (projection_status if projection_status in {
        "succeeded", "failed", "timed_out", "cancelled", "archived",
    } else None)
    projection_shape_valid = bool(
        instance is not None
        and set(body) == {"status", "context"}
        and safe_status == "failed"
        and set(context) == {"failure_code"}
        and safe_code is not None
        and safe_code == _runtime_failure_code(instance)
    )
    verification = result.verification_json if isinstance(result.verification_json, dict) else {}
    verification_source = verification.get("source")
    return {
        "projection_status": safe_status,
        "projection_failure_code": safe_code,
        "projection_only_stable_failure_code": projection_shape_valid,
        "verification_passed": verification.get("passed") is True,
        "verification_source": verification_source if verification_source == "runtime_failure" else None,
    }


def _safe_publication_facts(publication) -> dict:
    """只公开发布关联、必需状态及收据绑定校验，不回传任何业务收据正文。"""
    receipt = publication.receipt_json if isinstance(publication.receipt_json, dict) else {}
    receipt_shape_valid = (
        set(receipt) == {"projection", "execution_id", "result_id"}
        and receipt.get("projection") == "execution_result"
        and receipt.get("execution_id") == publication.execution_id
        and receipt.get("result_id") == publication.result_id
    )
    return {
        "id": publication.id,
        "execution_id": publication.execution_id,
        "result_id": publication.result_id,
        "target_type": publication.target_type if publication.target_type in {
            "application", "external_thread", "webhook",
        } else None,
        "required": publication.required is True,
        "status": publication.status if publication.status in {
            "pending", "delivering", "settled", "unknown", "dead_letter", "skipped",
        } else None,
        "settled_at_present": publication.settled_at is not None,
        "receipt_binding_valid": receipt_shape_valid,
    }


def _agent_branch_head_facts(branch, head, *, tenant_id: str, agent_id: str, knowledge_base_id: str) -> dict:
    """区分分支 head 版本匹配与该版本继承 overall 或专属 agent scope。"""
    branch_identity_valid = bool(
        branch
        and branch.tenant_id == tenant_id
        and branch.agent_id == agent_id
        and branch.knowledge_base_id == knowledge_base_id
        and branch.status == "active"
    )
    head_matches_branch = bool(
        branch_identity_valid
        and head
        and head.tenant_id == tenant_id
        and head.knowledge_base_id == knowledge_base_id
        and head.publication_state == "published"
        and head.version == branch.head_version
    )
    return {
        "database_agent_branch_present": branch_identity_valid,
        "database_agent_branch_agent_matches_fixture": bool(
            branch_identity_valid and branch.agent_id == agent_id
        ),
        "database_agent_content_revision": branch.content_revision if branch_identity_valid else None,
        "database_agent_head_version_id": head.id if head_matches_branch else None,
        "database_agent_head_version": head.version if head_matches_branch else None,
        "database_agent_head_scope": head.scope if head_matches_branch else None,
        "database_agent_head_matches_branch": head_matches_branch,
        "database_agent_head_scope_matches_agent": bool(
            head_matches_branch and head.scope == f"agent:{agent_id}"
        ),
        "database_agent_head_scope_inherits_overall": bool(
            head_matches_branch and head.scope == "overall"
        ),
    }


def _editor_baseline_facts(db, spec: LiveSpec, runtime: Path) -> dict:
    """按bootstrap中的精确文档身份核正文基线，只输出校验摘要而不返回正文。"""
    from sqlmodel import select
    from app.db.models import AgentKnowledgeBranch, KnowledgeBase, KnowledgeBaseVersion, KnowledgeDocument
    from app.knowledge.revisions import KnowledgeRevisionInputError, normalize_revision_source

    initial = _private_read(runtime / "initial.private.json", runtime)
    knowledge = initial.get("knowledge") if isinstance(initial.get("knowledge"), dict) else {}
    document_id = knowledge.get("old_document_id")
    knowledge_base_id = knowledge.get("kb_id")
    expected_version_id = knowledge.get("old_version_id")
    if not all(isinstance(value, str) and value for value in
               (document_id, knowledge_base_id, expected_version_id)):
        return {"document_present": False, "fixture_identity_complete": False}

    document = db.get(KnowledgeDocument, document_id)
    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    version = db.get(KnowledgeBaseVersion, expected_version_id)
    document_identity_valid = bool(
        document
        and document.id == document_id
        and document.tenant_id == spec.value["tenant"]["id"]
        and document.knowledge_base_id == knowledge_base_id
    )
    knowledge_base_identity_valid = bool(
        knowledge_base
        and knowledge_base.id == knowledge_base_id
        and knowledge_base.tenant_id == spec.value["tenant"]["id"]
    )
    version_identity_valid = bool(
        version
        and version.id == expected_version_id
        and version.tenant_id == spec.value["tenant"]["id"]
        and version.knowledge_base_id == knowledge_base_id
        and document
        and document.knowledge_base_version_id == expected_version_id
    )
    body_identity_valid = (
        document_identity_valid and knowledge_base_identity_valid and version_identity_valid
    )
    source = (
        document.normalized_source
        if body_identity_valid and isinstance(document.normalized_source, str)
        else None
    )
    expected_source = knowledge.get("old_content_md")
    try:
        canonical_source = normalize_revision_source(source) if source is not None else None
    except KnowledgeRevisionInputError:
        canonical_source = None
    try:
        canonical_expected_source = normalize_revision_source(expected_source) \
            if isinstance(expected_source, str) else None
    except KnowledgeRevisionInputError:
        canonical_expected_source = None
    current_version = ((knowledge_base.metadata_json or {}).get("current_version")
                       if knowledge_base_identity_valid else None)
    head_versions = db.exec(select(KnowledgeBaseVersion).where(
        KnowledgeBaseVersion.tenant_id == spec.value["tenant"]["id"],
        KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id,
        KnowledgeBaseVersion.version == current_version,
        KnowledgeBaseVersion.publication_state == "published",
    )).all() if isinstance(current_version, str) else []
    head = head_versions[0] if len(head_versions) == 1 else None
    agent_id = spec.value["agents"]["DTA"]["id"]
    agent_branches = db.exec(select(AgentKnowledgeBranch).where(
        AgentKnowledgeBranch.tenant_id == spec.value["tenant"]["id"],
        AgentKnowledgeBranch.agent_id == agent_id,
        AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
        AgentKnowledgeBranch.status == "active",
    )).all()
    agent_branch = agent_branches[0] if len(agent_branches) == 1 else None
    agent_head_versions = db.exec(select(KnowledgeBaseVersion).where(
        KnowledgeBaseVersion.tenant_id == spec.value["tenant"]["id"],
        KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id,
        KnowledgeBaseVersion.version == agent_branch.head_version,
        KnowledgeBaseVersion.publication_state == "published",
    )).all() if agent_branch else []
    agent_head = agent_head_versions[0] if len(agent_head_versions) == 1 else None
    source_kind = document.source_kind if body_identity_valid and isinstance(document.source_kind, str) \
        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", document.source_kind) else None
    checksum = document.source_checksum if body_identity_valid and isinstance(document.source_checksum, str) \
        and re.fullmatch(r"[a-f0-9]{64}", document.source_checksum) else None
    raw_scope = version.scope if body_identity_valid and isinstance(version.scope, str) else None
    version_scope = ("overall" if raw_scope == "overall" else
                     "agent" if raw_scope and raw_scope.startswith("agent:") else None)
    return {
        "document_present": document is not None,
        "fixture_identity_complete": True,
        "document_id": document.id if document else document_id,
        "document_id_matches_fixture": bool(document and document.id == document_id),
        "knowledge_base_matches_fixture": bool(document and document.knowledge_base_id == knowledge_base_id),
        "document_tenant_matches_fixture": bool(
            document and document.tenant_id == spec.value["tenant"]["id"]
        ),
        "document_body_identity_valid": body_identity_valid,
        "knowledge_base_tenant_matches_fixture": bool(
            knowledge_base and knowledge_base.tenant_id == spec.value["tenant"]["id"]
        ),
        "document_knowledge_base_matches_fixture": bool(
            document and document.knowledge_base_id == knowledge_base_id
        ),
        "version_tenant_matches_fixture": bool(
            version and version.tenant_id == spec.value["tenant"]["id"]
        ),
        "version_knowledge_base_matches_fixture": bool(
            version and version.knowledge_base_id == knowledge_base_id
        ),
        "document_version_id": document.knowledge_base_version_id
        if document_identity_valid else None,
        "document_version_matches_fixture": bool(
            document and document.knowledge_base_version_id == expected_version_id
        ),
        "version_scope": version_scope,
        "version_publication_state": (version.publication_state if body_identity_valid and version
                                       and version.publication_state
                                       in {"draft", "published", "archived"} else None),
        "document_source_kind": source_kind,
        "source_checksum": checksum,
        "normalized_source_sha256": hashlib.sha256(source.encode()).hexdigest() if source is not None else None,
        "normalized_source_json_sha256": hashlib.sha256(json.dumps(
            source, ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest() if source is not None else None,
        "canonical_source_json_sha256": hashlib.sha256(json.dumps(
            canonical_source, ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest() if canonical_source is not None else None,
        "normalized_source_length": len(source) if source is not None else None,
        "canonical_source_length": len(canonical_source) if canonical_source is not None else None,
        "normalized_source_exact_matches_fixture": (
            source == expected_source if source is not None and isinstance(expected_source, str) else None
        ),
        "normalized_source_matches_fixture": (
            canonical_source == canonical_expected_source
            if canonical_source is not None and canonical_expected_source is not None else None
        ),
        "database_current_version_id": current_version if isinstance(current_version, str) else None,
        "database_head_version_id": head.id if head else None,
        "database_head_version": head.version if head else None,
        "database_content_revision": knowledge_base.content_revision if knowledge_base else None,
        **_agent_branch_head_facts(
            agent_branch, agent_head, tenant_id=spec.value["tenant"]["id"],
            agent_id=agent_id, knowledge_base_id=knowledge_base_id,
        ),
        "head_version_id_matches_document": bool(
            head and document and head.id == document.knowledge_base_version_id
        ),
        "head_version_matches_document": bool(
            head and version and head.version == version.version
        ),
        "content_revision": knowledge_base.content_revision if knowledge_base else None,
    }


def _safe_event_error_code(payload: object) -> str | None:
    """只公开事件中的有界机器码，拒绝任意错误正文和非标识符内容。"""
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if not isinstance(code, str) or len(code) > 128:
        return None
    return code if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,127}", code) else None


def _safe_event_error_exception_category(payload: object) -> str | None:
    """从受限异常堆栈投影白名单类别，不回传异常文本、路径或模型正文。"""
    if not isinstance(payload, dict):
        return None
    traceback_text = payload.get("error_traceback")
    if not isinstance(traceback_text, str) or not traceback_text:
        return None
    safe_categories = {
        "BadRequestError": "provider_bad_request",
        "RateLimitError": "provider_rate_limited",
        "APITimeoutError": "provider_timeout",
        "TimeoutError": "provider_timeout",
        "APIConnectionError": "provider_transport_error",
        "ConnectError": "provider_transport_error",
        "ReadTimeout": "provider_transport_error",
        "APIStatusError": "provider_status_error",
        "JSONDecodeError": "invalid_model_output",
        "ValidationError": "invalid_model_output",
        "LLMError": "model_pipeline_error",
        "RuntimeError": "application_error",
        "ValueError": "application_error",
        "TypeError": "application_error",
    }
    for line in traceback_text[-8192:].splitlines():
        match = re.match(r"^\s*(?:[A-Za-z_]\w*\.)*([A-Za-z_]\w*(?:Error|Exception))(?::|$)", line)
        if match and match.group(1) in safe_categories:
            return safe_categories[match.group(1)]
    return "unclassified" if traceback_text.strip() else None


def snapshot(db, spec: LiveSpec, *, session_id: str | None = None) -> dict:
    """仅精确本租户/员工/会话原行白名单，不借外租户或最近实例。"""
    from sqlmodel import select
    from app.db.models import (
        AgentEvent, AgentKnowledgeBranch, AgentResourceBinding, ChatSession, ExecutionCommand,
        ExecutionPlanRevision, ExecutionPublication, ExecutionResult, ExecutionSignal, ExternalTaskEventInbox,
        GeneralSkill, GeneralSkillRevision,
        GeneralSkillUse, KnowledgeBaseVersion, KnowledgeChunk, KnowledgeConcept,
        KnowledgeDocument, KnowledgeIngestJob, MemoryCaptureJob, MemoryRecord, Message, SkillVersion, SopInstance,
        SopOperation, SopOperationAttempt, SopWorkItem,
    )

    value = spec.value
    tenant, owner = value["tenant"]["id"], value["owner"]["id"]
    agents = [agent["id"] for agent in value["agents"].values()]
    query = select(ChatSession).where(ChatSession.tenant_id == tenant, ChatSession.user_id == owner,
                                      ChatSession.agent_id.in_(agents))
    if session_id:
        query = query.where(ChatSession.id == session_id)
    sessions = db.exec(query).all()
    if session_id and not sessions:
        raise LiveFixtureError("F3_LIVE_SESSION_FORBIDDEN")
    session_ids = [row.id for row in sessions]
    instances = db.exec(select(SopInstance).where(SopInstance.tenant_id == tenant,
        SopInstance.agent_id.in_(agents), SopInstance.session_id.in_(session_ids),
        SopInstance.initiator_user_id == owner)).all() if sessions else []
    ids = [row.id for row in instances]
    instance_map = {row.id: row for row in instances}
    plans = db.exec(select(ExecutionPlanRevision).where(ExecutionPlanRevision.tenant_id == tenant,
        ExecutionPlanRevision.execution_id.in_(ids))).all() if ids else []
    signals = db.exec(select(ExecutionSignal).where(ExecutionSignal.tenant_id == tenant,
        ExecutionSignal.execution_id.in_(ids))).all() if ids else []
    version_ids = [row.skill_version_id for row in instances if row.skill_version_id]
    skill_versions = db.exec(select(SkillVersion).where(SkillVersion.tenant_id == tenant,
        SkillVersion.id.in_(version_ids))).all() if version_ids else []
    operations = db.exec(select(SopOperation).where(SopOperation.tenant_id == tenant,
        SopOperation.instance_id.in_(ids))).all() if ids else []
    op_ids = [row.id for row in operations]
    results = db.exec(select(ExecutionResult).where(ExecutionResult.tenant_id == tenant,
        ExecutionResult.execution_id.in_(ids))).all() if ids else []
    result_map = {row.id: row for row in results}
    publications = db.exec(select(ExecutionPublication).where(
        ExecutionPublication.tenant_id == tenant,
        ExecutionPublication.execution_id.in_(ids),
    )).all() if ids else []
    messages = db.exec(select(Message).where(Message.tenant_id == tenant,
        Message.session_id.in_(session_ids)).order_by(Message.created_at, Message.id)).all() if sessions else []
    user_message_ids = [row.id for row in messages if row.role == "user"]
    memory_jobs = db.exec(select(MemoryCaptureJob).where(
        MemoryCaptureJob.tenant_id == tenant,
        MemoryCaptureJob.user_id == owner,
        MemoryCaptureJob.source_message_id.in_(user_message_ids),
    )).all() if user_message_ids else []
    event_rows = db.exec(select(AgentEvent).where(
        AgentEvent.tenant_id == tenant, AgentEvent.session_id.in_(session_ids)
    )).all() if sessions else []
    message_rows = []
    for row in messages:
        metadata = row.metadata_json or {}
        instance = instance_map.get(metadata.get("execution_id"))
        result = result_map.get(metadata.get("result_id"))
        source = metadata.get("feedback_source")
        source_valid = bool(instance and isinstance(source, dict)
            and source.get("policy_version") == "feedback_source_v1"
            and source.get("execution_id") == instance.id
            and source.get("turn_id") == (metadata.get("user_message_id") or metadata.get("turn_id"))
            and instance.session_id == row.session_id)
        citations = []
        for citation in metadata.get("knowledge_citations") or []:
            document_id = citation.get("document_id")
            document = db.get(KnowledgeDocument, document_id) if isinstance(document_id, str) else None
            chunk_id = citation.get("chunk_id")
            chunk = db.get(KnowledgeChunk, chunk_id) if isinstance(chunk_id, str) else None
            concept_id = citation.get("concept_id")
            has_concept_identity = isinstance(concept_id, str) and bool(concept_id)
            concept = None
            if has_concept_identity:
                concept = db.exec(select(KnowledgeConcept).where(
                    KnowledgeConcept.tenant_id == tenant,
                    KnowledgeConcept.id == concept_id,
                )).first()
                if concept is None:
                    matches = db.exec(select(KnowledgeConcept).where(
                        KnowledgeConcept.tenant_id == tenant,
                        KnowledgeConcept.concept_id == concept_id,
                    )).all()
                    concept = matches[0] if len(matches) == 1 else None
            if has_concept_identity:
                version = db.get(KnowledgeBaseVersion, concept.knowledge_base_version_id) \
                    if concept and isinstance(concept.knowledge_base_version_id, str) else None
                valid = bool(
                    concept
                    and concept.tenant_id == tenant
                    and concept.status == "active"
                    and version
                    and version.tenant_id == tenant
                    and version.knowledge_base_id == concept.knowledge_base_id
                    and version.publication_state == "published"
                )
                version_ids = [concept.knowledge_base_version_id] if valid else []
            else:
                valid = bool(
                    document
                    and document.tenant_id == tenant
                    and (not chunk_id or chunk and chunk.tenant_id == tenant
                         and chunk.document_id == document.id)
                )
                version_ids = [document.knowledge_base_version_id] if valid and document else []
            citations.append({key: citation.get(key) for key in (
                "id", "kind", "concept_id", "document_id", "bucket_id", "chunk_id")}
                | {"source_valid": valid, "version_ids": version_ids,
                   "facts": _facts(str(citation.get("content") or ""), spec)})
        binding = bool(instance and result and instance.kind == "dynamic_task" and instance.status == "succeeded"
            and instance.current_result_id == result.id and result.execution_id == instance.id
            and result.status == "verified" and (result.verification_json or {}).get("passed") is True
            and metadata.get("result_checksum") == result.checksum
            and row.content == (result.result_json or {}).get("markdown"))
        message_rows.append(_fields(row, ("id", "tenant_id", "session_id", "role", "created_at")) | {
            "content_sha256": digest(row.content), "facts": _facts(row.content, spec),
            "business_outcome": _business_outcome(row.content),
            "content_hash": digest(row.content), "time_context": metadata.get("time_context"),
            "execution_status": source.get("execution_status") if source_valid else None,
            "receipt_ids": re.findall(r"f3-receipt-[0-9a-f]+", row.content),
            **{key: metadata.get(key) for key in ("execution_id", "user_message_id", "turn_id", "client_turn_id")},
            "metadata": {key: metadata.get(key) for key in ("user_message_id", "turn_id", "client_turn_id",
                "execution_id", "result_id", "execution_engine", "time_context")}, "citations": citations,
            "result_id": metadata.get("result_id"), "result_status": result.status if result else None,
            "result_binding_valid": binding})
    operation_rows = []
    for row in operations:
        retrieval = []
        result_summary = _knowledge_result_summary(row.result_json)
        for source in _walk(row.result_json):
            if source.get("outcome") in {"no_match", "insufficient"} and isinstance(
                source.get("knowledge_base_version_ids"), list
            ):
                version_ids = source["knowledge_base_version_ids"]
                version_rows = [db.get(KnowledgeBaseVersion, identity)
                    for identity in version_ids if isinstance(identity, str)]
                retrieval.append({"outcome": source["outcome"], "version_ids": version_ids,
                    "evidence_required": result_summary["evidence_required"],
                    "evidence_satisfied": result_summary["evidence_satisfied"],
                    "version_identity_valid": bool(version_rows) and len(version_rows) == len(version_ids)
                        and all(version and version.tenant_id == tenant for version in version_rows),
                    "evidence_present": bool(source.get("chunks") or source.get("evidence_pack")
                        or source.get("okf_citations") or source.get("selected_documents")),
                    "degraded": source.get("degraded") is True})
            if not source.get("document_id"):
                continue
            document = db.get(KnowledgeDocument, source["document_id"])
            chunk_reference_present = "chunk_id" in source or source.get("chunk_index") is not None
            chunk_id = source.get("chunk_id") or (
                source.get("id") if source.get("chunk_index") is not None else None
            )
            chunk = db.get(KnowledgeChunk, chunk_id) if chunk_id else None
            valid_document = bool(document and document.tenant_id == tenant)
            valid_chunk = bool(
                not chunk_reference_present
                or chunk
                and document
                and chunk.document_id == document.id
                and chunk.tenant_id == tenant
            )
            valid = valid_document and valid_chunk
            retrieval.append({"version_ids": [document.knowledge_base_version_id] if document else [],
                "outcome": source.get("outcome") or result_summary["outcome"],
                "evidence_required": result_summary["evidence_required"],
                "evidence_satisfied": result_summary["evidence_satisfied"], "identity_valid": valid,
                "evidence_facts": _facts(chunk.content if valid and chunk else "", spec)})
        operation_rows.append(_fields(row, ("id", "tenant_id", "instance_id", "node_execution_id",
            "operation_name", "status", "effect_kind", "effect_state", "request_fingerprint",
            "capability_checksum", "approval_work_item_id", "approval_fingerprint", "approved_by_user_id",
            "approved_at", "external_task_id", "external_submission_id", "external_observation_revision",
            "cancellation_disposition", "logical_action_id")) | {
            "remote_key_hash": digest(row.remote_idempotency_key),
            "remote_idempotency_key_hash": hashlib.sha256(row.remote_idempotency_key.encode()).hexdigest()
                if row.remote_idempotency_key is not None else None,
            "request_sha256": digest(row.request_json),
            "request_json": _business(row.request_json), "result_sha256": digest(row.result_json),
            "knowledge_result_outcome": result_summary["outcome"],
            "knowledge_result_evidence_required": result_summary["evidence_required"],
            "knowledge_result_evidence_satisfied": result_summary["evidence_satisfied"],
            "knowledge_result_version_id_count": sum(
                len(item.get("knowledge_base_version_ids", []))
                for item in _walk(row.result_json)
                if isinstance(item.get("knowledge_base_version_ids"), list)
            ),
            "result": next((_business(item) for item in _walk(row.result_json) if item.get("receipt_id")), {}),
            "error_code": (row.error_json or {}).get("code"), "retrieval": retrieval})
    def related(model, column, identifiers, keys):
        """按原tenant和精确关联主键读取指定账本白名单，空范围不取全表。"""
        rows = db.exec(select(model).where(model.tenant_id == tenant, column.in_(identifiers))).all() if identifiers else []
        return [_fields(row, keys) for row in rows]

    knowledge_bases = db.exec(select(KnowledgeDocument).where(KnowledgeDocument.tenant_id == tenant)).all()
    kb_ids = list({row.knowledge_base_id for row in knowledge_bases})
    versions = related(KnowledgeBaseVersion, KnowledgeBaseVersion.knowledge_base_id, kb_ids,
                       ("id", "knowledge_base_id", "version", "scope", "status", "publication_state"))
    branches = related(AgentKnowledgeBranch, AgentKnowledgeBranch.knowledge_base_id, kb_ids,
                       ("id", "agent_id", "knowledge_base_id", "head_version", "content_revision", "status"))
    jobs = related(KnowledgeIngestJob, KnowledgeIngestJob.knowledge_base_id, kb_ids,
                   ("id", "status", "document_id", "published_document_id", "published_version_id", "job_kind"))
    current_versions = [version["id"] for row in branches
                        if row["agent_id"] == value["agents"]["DTA"]["id"] and row["status"] == "active"
                        for version in versions if version["knowledge_base_id"] == row["knowledge_base_id"]
                        and version["version"] == row["head_version"]
                        and version["publication_state"] == "published"]
    guidance_resources = []
    guidance_bindings = db.exec(select(AgentResourceBinding).where(
        AgentResourceBinding.tenant_id == tenant, AgentResourceBinding.agent_id.in_(agents),
        AgentResourceBinding.resource_type == "general_skill", AgentResourceBinding.status == "active",
    )).all()
    for binding in guidance_bindings:
        skill = db.get(GeneralSkill, binding.resource_id)
        if skill is None or skill.tenant_id != tenant:
            continue
        revision_id = (binding.metadata_json or {}).get("pinned_revision_id") or skill.current_published_revision_id
        revision = db.get(GeneralSkillRevision, revision_id) if revision_id else None
        if revision is None or revision.tenant_id != tenant or revision.skill_id != skill.id:
            continue
        guidance_resources.append({"skill_id": skill.id, "revision_id": revision.id,
            "agent_id": binding.agent_id, "slug": skill.slug, "usage_mode": skill.usage_mode,
            "status": revision.status, "content_checksum": revision.content_checksum,
            "markdown_hash": digest(revision.normalized_skill_markdown),
            "native_wait_contract": all(word in revision.normalized_skill_markdown
                for word in ("clarification", "wait_input", "completed_steps"))
                and "ask_user" not in revision.normalized_skill_markdown})
    guidance_uses = db.exec(select(GeneralSkillUse).where(GeneralSkillUse.tenant_id == tenant,
        GeneralSkillUse.user_id == owner, GeneralSkillUse.agent_id.in_(agents),
        GeneralSkillUse.session_id.in_(session_ids))).all() if session_ids else []
    memories = db.exec(select(MemoryRecord).where(MemoryRecord.tenant_id == tenant,
        MemoryRecord.agent_id.in_(agents), MemoryRecord.user_id == owner)).all()
    runtime = Path(os.environ.get("F3_LIVE_RUNTIME", ""))
    wire_rows = []
    for path in runtime.glob("live-wire-*.private.jsonl"):
        for line in path.read_text().splitlines():
            wire_rows.append(json.loads(line))
    sdk_calls = []
    phases = ("dynamic_task.answer", "dynamic_task.answer_repair", "dynamic_task.plan",
              "router.scene", "response.generate", "response.generate_stream", "reflection.review",
              "step.decide", "step.repair", "llm.request")
    identity_rows = {"source_input_id": [row.id for row in messages if row.role == "user"],
                     "turn_id": [row.id for row in messages if row.role == "user"],
                     "session_id": session_ids, "instance_id": ids,
                     "tenant_id": [tenant], "user_id": [owner], "agent_id": agents,
                     "task_id": [row.id for row in memory_jobs]}
    consumed = set()
    for path in runtime.glob("sdk-observations-*.safe.jsonl"):
        for line in path.read_text().splitlines():
            receipt = json.loads(line)
            candidates = [(index, row) for index, row in enumerate(wire_rows)
                          if index not in consumed and row.get("sdk_request_sha256") == receipt.get("request_sha256")
                          and row.get("pid") == receipt.get("pid")
                          and row.get("identity_sha256") == receipt.get("identity_sha256")]
            wire = candidates[0][1] if candidates else {}
            if candidates:
                consumed.add(candidates[0][0])
            hashes = receipt.get("identity_sha256") or {}
            bindings = {key: next((identity for identity in values if digest(identity) == hashes.get(key)), None)
                        for key, values in identity_rows.items()}
            bindings, binding_valid = _persistent_sdk_bindings(hashes, bindings,
                tenant=tenant, owner=owner, instances=instances, sessions=sessions,
                messages=messages, plans=plans, signals=signals, versions=skill_versions,
                memory_jobs=memory_jobs)
            sdk_calls.append({**receipt, **bindings, "operation": next((phase for phase in phases
                if digest(phase) == receipt.get("phase_sha256")), None),
                "actual_sdk_transport": receipt.get("observation_valid") is True,
                "actual_provider_call": receipt.get("observation_valid") is True,
                "wire_binding_valid": bool(candidates),
                "persistent_binding_valid": binding_valid,
                "knowledge_old_complete": wire.get("knowledge_old_complete", False),
                "knowledge_new_complete": wire.get("knowledge_new_complete", False),
                "memory_complete": wire.get("memory_complete", []),
                "has_knowledge_result": wire.get("has_knowledge_result", False),
                "knowledge_version_ids": wire.get("knowledge_version_ids", [])})
    plan_by_instance = {row.execution_id: row for row in plans
                        if row.id in {instance.current_plan_revision_id for instance in instances}}
    return {"pid": os.getpid(), "provider_kind": "simulated_async_service", "real_vendor_qualified": False,
        "sessions": [_fields(row, ("id", "tenant_id", "user_id", "agent_id", "status")) for row in sessions],
        "instances": [_fields(row, ("id", "tenant_id", "agent_id", "initiator_user_id", "session_id", "kind",
            "status", "revision", "skill_id", "skill_version_id", "definition_checksum", "current_node_id",
            "current_plan_revision_id", "current_result_id", "effect_state", "source_ref", "source_kind",
            "cancellation_disposition", "cancellation_requested_at")) | {
                "plan_checksum": row.current_plan_checksum,
                "source_input_id": (row.context_json or {}).get("source_input_id"),
                "time_context": (row.context_json or {}).get("time_context"),
                "frozen_knowledge_versions": [card["version_id"]
                    for capability in (row.capability_snapshot_json or {}).get("knowledge", [])
                    if isinstance(capability, dict)
                    for card in (capability.get("model_view") or {}).get("knowledge_bases", [])
                    if isinstance(card, dict) and isinstance(card.get("version_id"), str)],
                "metadata": {key: (row.context_json or {}).get(key) for key in
                             ("time_context", "source_input_id", "source_message_id")},
                "runtime_failure_code": _runtime_failure_code(row),
                **(_expected_async_tool_facts(
                    row, plan_by_instance.get(row.id), spec.value["tool"]["name"]
                ) if row.kind == "dynamic_task" else {})}
            for row in instances], "operations": operation_rows, "messages": message_rows,
        "attempts": related(SopOperationAttempt, SopOperationAttempt.operation_id, op_ids,
                            ("id", "operation_id", "status", "attempt_number")),
        "plan_revisions": [_fields(row, ("id", "tenant_id", "execution_id", "parent_revision_id",
                                         "checksum", "status")) for row in plans],
        "signals": [_fields(row, ("id", "tenant_id", "execution_id", "signal_type", "status",
                                  "causation_id", "consumed_at")) for row in signals],
        "inbox": related(ExternalTaskEventInbox, ExternalTaskEventInbox.operation_id, op_ids,
                         ("id", "operation_id", "status", "provider_task_id", "provider_event_id",
                          "provider_status", "signal_id", "observation_revision")),
        "attention": [_fields(item, ("id", "instance_id", "attention_kind", "status", "revision", "title"))
            | {"execution_id": item.instance_id, "kind": item.attention_kind,
               "session_id": instance_map[item.instance_id].session_id,
               "payload": item.payload_json, "allowed_commands": item.allowed_commands_json,
               "available_commands": item.allowed_commands_json}
            for item in db.exec(select(SopWorkItem).where(SopWorkItem.tenant_id == tenant,
                SopWorkItem.instance_id.in_(ids))).all()] if ids else [],
        "commands": related(ExecutionCommand, ExecutionCommand.execution_id, ids,
                            ("id", "execution_id", "command_type", "actor_user_id", "status", "consumed_at")),
        "events": [
            _fields(row, ("id", "session_id", "event_type", "aggregate_id", "causation_id"))
            | ({"error_code": _safe_event_error_code(row.payload_json),
                "error_exception_category": _safe_event_error_exception_category(row.payload_json)}
               if row.event_type == "error_occurred" else {})
            for row in event_rows
        ],
        "results": [
            _fields(row, ("id", "execution_id", "status", "checksum"))
            | _safe_terminal_result_facts(row, instance_map.get(row.execution_id))
            for row in results
        ],
        "publications": [_safe_publication_facts(row) for row in publications],
        "editor_baseline": _editor_baseline_facts(db, spec, runtime),
        "sdk_calls": sdk_calls, "knowledge": {"versions": versions, "branches": branches,
            "current_version_id": current_versions[0] if len(current_versions) == 1 else None,
            "old_version_id": versions[0]["id"] if versions else None}, "jobs": jobs,
        "guidance_resources": guidance_resources,
        "guidance_uses": [_fields(use, ("id", "session_id", "turn_id", "execution_id", "agent_id",
            "user_id", "skill_id", "revision_id", "content_checksum", "status")) for use in guidance_uses],
        "memories": [_fields(row, ("id", "agent_id", "user_id", "kind", "capture_job_id"))
                     | {"content_sha256": digest(row.content),
                        "manual_complete": row.content in [item["content"] for item in value["memories"]]}
                     for row in memories]}


def install_api(application, runtime: Path) -> None:
    """仅当前私有测试环境添加受控GET观察，不安装模型或provider业务路由。"""
    from fastapi import Header, HTTPException
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse, Response

    spec = _assert_isolated(runtime)

    def authorized(key: str | None) -> None:
        """私有观察key采用恒定时间核对，失败时不读取应用数据库。"""
        if not key or not hmac.compare_digest(key, spec.value["control_key"]):
            raise HTTPException(status_code=403, detail="F3_LIVE_CONTROL_FORBIDDEN")

    @application.get("/__e2e/f3/bootstrap")
    @application.get("/__e2e/f3/initial")
    def initial(x_f3_control_key: str | None = Header(None)):
        """只返回当前自有合成账号和已发布资源的私有初始化投影。"""
        authorized(x_f3_control_key)
        try:
            _, body = _private_file_read(runtime / "initial.private.json", runtime)
        except LiveFixtureLimitError as error:
            raise HTTPException(status_code=413, detail="F3_LIVE_BOOTSTRAP_LIMIT") from error
        except LiveFixtureError as error:
            raise HTTPException(status_code=503, detail="F3_LIVE_BOOTSTRAP_UNAVAILABLE") from error
        return Response(content=body, media_type="application/json",
                        headers={"Cache-Control": "no-store"})

    @application.get("/__e2e/f3/facts")
    def facts(session_id: str | None = None, x_f3_control_key: str | None = Header(None)):
        """本次原持久账本只读观察，不触发模型、审批或工具操作。"""
        authorized(x_f3_control_key)
        from sqlmodel import Session
        from app.db import engine

        with Session(engine) as db:
            try:
                value = snapshot(db, spec, session_id=session_id)
            except LiveFixtureError as error:
                raise HTTPException(status_code=403, detail=str(error)) from error
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    @application.get("/__e2e/f3/sdk-health")
    def sdk_health(x_f3_control_key: str | None = Header(None)):
        """只读原SDK与输入观察器当前健康，不把在途请求改成已结算。"""
        authorized(x_f3_control_key)
        ledger = getattr(application.state, "f3_sdk_observation_ledger", None)
        observer = getattr(application.state, "f3_live_wire_observer", None)
        if ledger is None or observer is None:
            raise HTTPException(status_code=503, detail="F3_LIVE_SDK_OBSERVER_UNAVAILABLE")
        health = {"pid": os.getpid(), "original_sdk": ledger.health(),
                  "wire_inputs": observer.health()}
        if "memory_quality" in spec.value:
            memory = getattr(application.state, "f3_live_memory_observer", None)
            if memory is None:
                raise HTTPException(status_code=503, detail="F3_LIVE_MEMORY_OBSERVER_UNAVAILABLE")
            health["memory_inputs"] = memory.health()
        return JSONResponse(health, headers={"Cache-Control": "no-store"})

    @application.get("/__e2e/f3/approval/{attention_id}")
    def approval(attention_id: str, x_f3_control_key: str | None = Header(None)):
        """读取原Attention前后冻结字段，缺项等待消费者写回而非伪造批准。"""
        authorized(x_f3_control_key)
        from sqlmodel import Session
        from app.db import engine

        with Session(engine) as db:
            try:
                value = approval_projection(db, spec, attention_id)
            except LiveFixtureError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
        return JSONResponse(jsonable_encoder(value), headers={"Cache-Control": "no-store"})

    if "memory_quality" in spec.value:
        from final_f3_live_memory import install_api as install_memory_api

        install_memory_api(application, runtime, spec.value)
