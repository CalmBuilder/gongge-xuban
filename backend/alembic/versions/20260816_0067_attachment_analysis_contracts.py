"""
@Time       : 2026/08/13 19:59
@Author     : zhanglp8181
@File       : bf676d1fd7e7_attachment_analysis_contracts.py
@CallChain  : Alembic upgrade/downgrade → 附件资源/提取/快照/外发事实
@Description: 建立附件 A++ 所需的不可变提取、权威消息引用、Turn 快照和外发审计表。

Revision ID: 20260816_0067
Revises: 20260815_0066
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260816_0067"
down_revision: str | None = "20260815_0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建附件分析契约表，并为既有资源和 Execution 快照增加正交身份字段。"""

    op.create_table('draft_upload_bindings',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('binding_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('owner_user_id', sa.String(length=128), nullable=False),
    sa.Column('agent_id', sa.String(length=128), nullable=False),
    sa.Column('session_id', sa.String(length=128), nullable=True),
    sa.Column('draft_conversation_id', sa.String(length=128), nullable=True),
    sa.Column('nonce_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('idempotency_key', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('resource_set_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('lease_owner', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
    sa.Column('expires_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
    sa.Column('claimed_at', sa.DateTime(), nullable=True),
    sa.Column('consumed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("status IN ('active', 'claimed', 'consumed', 'expired')", name='ck_draft_upload_binding_status'),
    sa.CheckConstraint('NOT (session_id IS NULL AND draft_conversation_id IS NULL)', name='ck_draft_upload_binding_target'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'binding_id', name='uq_draft_upload_binding_identity'),
    sa.UniqueConstraint('tenant_id', 'idempotency_key', name='uq_draft_upload_idempotency')
    )
    op.create_index(op.f('ix_draft_upload_bindings_agent_id'), 'draft_upload_bindings', ['agent_id'], unique=False)
    op.create_index(op.f('ix_draft_upload_bindings_binding_id'), 'draft_upload_bindings', ['binding_id'], unique=False)
    op.create_index(op.f('ix_draft_upload_bindings_draft_conversation_id'), 'draft_upload_bindings', ['draft_conversation_id'], unique=False)
    op.create_index(op.f('ix_draft_upload_bindings_expires_at'), 'draft_upload_bindings', ['expires_at'], unique=False)
    op.create_index(op.f('ix_draft_upload_bindings_owner_user_id'), 'draft_upload_bindings', ['owner_user_id'], unique=False)
    op.create_index(op.f('ix_draft_upload_bindings_session_id'), 'draft_upload_bindings', ['session_id'], unique=False)
    op.create_index(op.f('ix_draft_upload_bindings_status'), 'draft_upload_bindings', ['status'], unique=False)
    op.create_index(op.f('ix_draft_upload_bindings_tenant_id'), 'draft_upload_bindings', ['tenant_id'], unique=False)
    op.create_table('input_document_elements',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('extraction_id', sa.String(length=128), nullable=False),
    sa.Column('element_index', sa.Integer(), nullable=False),
    sa.Column('element_type', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('text', sa.Text().with_variant(mysql.LONGTEXT(), 'mysql'), nullable=True),
    sa.Column('table_json', sa.JSON(), nullable=True),
    sa.Column('locator_json', sa.JSON(), nullable=False),
    sa.Column('content_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('char_count', sa.Integer(), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=False),
    sa.Column('column_count', sa.Integer(), nullable=False),
    sa.Column('truncated', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'extraction_id', 'element_index', name='uq_input_document_element_index')
    )
    op.create_index(op.f('ix_input_document_elements_content_checksum'), 'input_document_elements', ['content_checksum'], unique=False)
    op.create_index(op.f('ix_input_document_elements_element_type'), 'input_document_elements', ['element_type'], unique=False)
    op.create_index(op.f('ix_input_document_elements_extraction_id'), 'input_document_elements', ['extraction_id'], unique=False)
    op.create_index(op.f('ix_input_document_elements_tenant_id'), 'input_document_elements', ['tenant_id'], unique=False)
    op.create_table('input_resource_extraction_attempts',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('resource_id', sa.String(length=128), nullable=False),
    sa.Column('resource_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('parser_name', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('parser_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('parser_config_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('lease_owner', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
    sa.Column('fencing_token', sa.Integer(), nullable=False),
    sa.Column('lease_expires_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
    sa.Column('retry_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
    sa.Column('temporary_manifest_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('error_code', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('error_detail_json', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.CheckConstraint("status IN ('pending', 'claimed', 'running', 'succeeded', 'failed', 'cancelled', 'dead_letter')", name='ck_input_extraction_attempt_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'resource_id', 'resource_version', 'parser_config_checksum', 'attempt_no', name='uq_input_extraction_attempt')
    )
    op.create_index(op.f('ix_input_resource_extraction_attempts_lease_expires_at'), 'input_resource_extraction_attempts', ['lease_expires_at'], unique=False)
    op.create_index(op.f('ix_input_resource_extraction_attempts_lease_owner'), 'input_resource_extraction_attempts', ['lease_owner'], unique=False)
    op.create_index(op.f('ix_input_resource_extraction_attempts_resource_id'), 'input_resource_extraction_attempts', ['resource_id'], unique=False)
    op.create_index(op.f('ix_input_resource_extraction_attempts_retry_at'), 'input_resource_extraction_attempts', ['retry_at'], unique=False)
    op.create_index(op.f('ix_input_resource_extraction_attempts_status'), 'input_resource_extraction_attempts', ['status'], unique=False)
    op.create_index(op.f('ix_input_resource_extraction_attempts_tenant_id'), 'input_resource_extraction_attempts', ['tenant_id'], unique=False)
    op.create_table('input_resource_extractions',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('resource_id', sa.String(length=128), nullable=False),
    sa.Column('resource_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('content_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('parser_name', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('parser_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('parser_config_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('extraction_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('element_manifest_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('published_from_attempt_id', sa.String(length=128), nullable=False),
    sa.Column('element_count', sa.Integer(), nullable=False),
    sa.Column('page_count', sa.Integer(), nullable=False),
    sa.Column('sheet_count', sa.Integer(), nullable=False),
    sa.Column('slide_count', sa.Integer(), nullable=False),
    sa.Column('metadata_json', sa.JSON(), nullable=True),
    sa.Column('published_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'published_from_attempt_id', name='uq_input_resource_extraction_attempt'),
    sa.UniqueConstraint('tenant_id', 'resource_id', 'resource_version', 'parser_config_checksum', 'extraction_checksum', name='uq_input_resource_extraction_content')
    )
    op.create_index(op.f('ix_input_resource_extractions_element_manifest_checksum'), 'input_resource_extractions', ['element_manifest_checksum'], unique=False)
    op.create_index(op.f('ix_input_resource_extractions_extraction_checksum'), 'input_resource_extractions', ['extraction_checksum'], unique=False)
    op.create_index(op.f('ix_input_resource_extractions_published_from_attempt_id'), 'input_resource_extractions', ['published_from_attempt_id'], unique=False)
    op.create_index(op.f('ix_input_resource_extractions_resource_id'), 'input_resource_extractions', ['resource_id'], unique=False)
    op.create_index(op.f('ix_input_resource_extractions_tenant_id'), 'input_resource_extractions', ['tenant_id'], unique=False)
    op.create_table('message_input_resource_links',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('session_id', sa.String(length=128), nullable=False),
    sa.Column('message_id', sa.String(length=128), nullable=False),
    sa.Column('resource_binding_id', sa.String(length=128), nullable=False),
    sa.Column('resource_id', sa.String(length=128), nullable=False),
    sa.Column('resource_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('content_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'message_id', 'resource_id', 'resource_version', name='uq_message_input_resource_link')
    )
    op.create_index(op.f('ix_message_input_resource_links_message_id'), 'message_input_resource_links', ['message_id'], unique=False)
    op.create_index(op.f('ix_message_input_resource_links_resource_binding_id'), 'message_input_resource_links', ['resource_binding_id'], unique=False)
    op.create_index(op.f('ix_message_input_resource_links_resource_id'), 'message_input_resource_links', ['resource_id'], unique=False)
    op.create_index(op.f('ix_message_input_resource_links_session_id'), 'message_input_resource_links', ['session_id'], unique=False)
    op.create_index(op.f('ix_message_input_resource_links_tenant_id'), 'message_input_resource_links', ['tenant_id'], unique=False)
    op.create_table('message_input_binding_links',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('execution_id', sa.String(length=128), nullable=False),
    sa.Column('definition_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('slot_key', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('message_resource_link_id', sa.String(length=128), nullable=False),
    sa.Column('input_snapshot_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'execution_id', 'message_resource_link_id', name='uq_message_input_binding_resource'),
    sa.UniqueConstraint('tenant_id', 'execution_id', 'slot_key', 'ordinal', name='uq_message_input_binding_slot_ordinal')
    )
    op.create_index(op.f('ix_message_input_binding_links_execution_id'), 'message_input_binding_links', ['execution_id'], unique=False)
    op.create_index(op.f('ix_message_input_binding_links_input_snapshot_id'), 'message_input_binding_links', ['input_snapshot_id'], unique=False)
    op.create_index(op.f('ix_message_input_binding_links_message_resource_link_id'), 'message_input_binding_links', ['message_resource_link_id'], unique=False)
    op.create_index(op.f('ix_message_input_binding_links_slot_key'), 'message_input_binding_links', ['slot_key'], unique=False)
    op.create_index(op.f('ix_message_input_binding_links_tenant_id'), 'message_input_binding_links', ['tenant_id'], unique=False)
    op.create_table('provider_input_dispatch_groups',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('consumer_kind', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('causation_id', sa.String(length=128), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('ordered_receipt_ids_json', sa.JSON(), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('settled_at', sa.DateTime(), nullable=True),
    sa.CheckConstraint("status IN ('prepared', 'dispatching', 'delivered', 'failed_pre_send', 'unknown', 'settled', 'discarded')", name='ck_provider_dispatch_group_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'causation_id', 'attempt_no', name='uq_provider_dispatch_group')
    )
    op.create_index(op.f('ix_provider_input_dispatch_groups_causation_id'), 'provider_input_dispatch_groups', ['causation_id'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_groups_consumer_kind'), 'provider_input_dispatch_groups', ['consumer_kind'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_groups_status'), 'provider_input_dispatch_groups', ['status'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_groups_tenant_id'), 'provider_input_dispatch_groups', ['tenant_id'], unique=False)
    op.create_table('provider_input_dispatch_receipts',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('dispatch_group_id', sa.String(length=128), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('consumer_kind', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('turn_id', sa.String(length=128), nullable=True),
    sa.Column('execution_id', sa.String(length=128), nullable=True),
    sa.Column('operation_id', sa.String(length=128), nullable=True),
    sa.Column('resource_id', sa.String(length=128), nullable=False),
    sa.Column('extraction_id', sa.String(length=128), nullable=False),
    sa.Column('slice_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('expected_acl_revision', sa.Integer(), nullable=False),
    sa.Column('egress_policy_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('dispatch_token', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('lease_owner', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
    sa.Column('fencing_token', sa.Integer(), nullable=False),
    sa.Column('deadline_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
    sa.Column('provider_request_id', sa.String(length=128), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('causation_id', sa.String(length=128), nullable=False),
    sa.Column('prepared_at', sa.DateTime(), nullable=False),
    sa.Column('dispatch_started_at', sa.DateTime(), nullable=True),
    sa.Column('settled_at', sa.DateTime(), nullable=True),
    sa.CheckConstraint("status IN ('prepared', 'dispatching', 'delivered', 'failed_pre_send', 'unknown', 'settled', 'discarded')", name='ck_provider_dispatch_receipt_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'dispatch_group_id', 'resource_id', 'attempt_no', name='uq_provider_dispatch_resource_attempt'),
    sa.UniqueConstraint('tenant_id', 'dispatch_token', name='uq_provider_dispatch_token')
    )
    op.create_index(op.f('ix_provider_input_dispatch_receipts_causation_id'), 'provider_input_dispatch_receipts', ['causation_id'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_consumer_kind'), 'provider_input_dispatch_receipts', ['consumer_kind'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_deadline_at'), 'provider_input_dispatch_receipts', ['deadline_at'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_dispatch_group_id'), 'provider_input_dispatch_receipts', ['dispatch_group_id'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_dispatch_token'), 'provider_input_dispatch_receipts', ['dispatch_token'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_execution_id'), 'provider_input_dispatch_receipts', ['execution_id'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_extraction_id'), 'provider_input_dispatch_receipts', ['extraction_id'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_lease_owner'), 'provider_input_dispatch_receipts', ['lease_owner'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_operation_id'), 'provider_input_dispatch_receipts', ['operation_id'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_resource_id'), 'provider_input_dispatch_receipts', ['resource_id'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_status'), 'provider_input_dispatch_receipts', ['status'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_tenant_id'), 'provider_input_dispatch_receipts', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_provider_input_dispatch_receipts_turn_id'), 'provider_input_dispatch_receipts', ['turn_id'], unique=False)
    op.create_table('resource_session_bindings',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('resource_id', sa.String(length=128), nullable=False),
    sa.Column('resource_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('owner_user_id', sa.String(length=128), nullable=False),
    sa.Column('session_id', sa.String(length=128), nullable=False),
    sa.Column('agent_id', sa.String(length=128), nullable=False),
    sa.Column('upload_binding_id', sa.String(length=128), nullable=True),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'resource_id', 'resource_version', name='uq_resource_session_binding_resource')
    )
    op.create_index(op.f('ix_resource_session_bindings_agent_id'), 'resource_session_bindings', ['agent_id'], unique=False)
    op.create_index(op.f('ix_resource_session_bindings_owner_user_id'), 'resource_session_bindings', ['owner_user_id'], unique=False)
    op.create_index(op.f('ix_resource_session_bindings_resource_id'), 'resource_session_bindings', ['resource_id'], unique=False)
    op.create_index(op.f('ix_resource_session_bindings_session_id'), 'resource_session_bindings', ['session_id'], unique=False)
    op.create_index(op.f('ix_resource_session_bindings_tenant_id'), 'resource_session_bindings', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_resource_session_bindings_upload_binding_id'), 'resource_session_bindings', ['upload_binding_id'], unique=False)
    op.create_table('scanner_evidence',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('resource_id', sa.String(length=128), nullable=False),
    sa.Column('resource_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('assurance_level', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('engine', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('engine_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('definition_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('definition_published_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
    sa.Column('scanned_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=False),
    sa.Column('freshness_policy_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('max_age_at_scan_seconds', sa.Integer(), nullable=False),
    sa.Column('verdict', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('evidence_json', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'resource_id', 'resource_version', 'engine', 'definition_version', name='uq_scanner_evidence_definition')
    )
    op.create_index(op.f('ix_scanner_evidence_assurance_level'), 'scanner_evidence', ['assurance_level'], unique=False)
    op.create_index(op.f('ix_scanner_evidence_resource_id'), 'scanner_evidence', ['resource_id'], unique=False)
    op.create_index(op.f('ix_scanner_evidence_tenant_id'), 'scanner_evidence', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_scanner_evidence_verdict'), 'scanner_evidence', ['verdict'], unique=False)
    op.create_table('selected_resource_extractions',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('resource_id', sa.String(length=128), nullable=False),
    sa.Column('resource_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('profile_key', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('extraction_id', sa.String(length=128), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'resource_id', 'resource_version', 'profile_key', name='uq_selected_resource_extraction_profile')
    )
    op.create_index(op.f('ix_selected_resource_extractions_extraction_id'), 'selected_resource_extractions', ['extraction_id'], unique=False)
    op.create_index(op.f('ix_selected_resource_extractions_profile_key'), 'selected_resource_extractions', ['profile_key'], unique=False)
    op.create_index(op.f('ix_selected_resource_extractions_resource_id'), 'selected_resource_extractions', ['resource_id'], unique=False)
    op.create_index(op.f('ix_selected_resource_extractions_tenant_id'), 'selected_resource_extractions', ['tenant_id'], unique=False)
    op.create_table('turn_input_read_receipts',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('turn_id', sa.String(length=128), nullable=False),
    sa.Column('snapshot_id', sa.String(length=128), nullable=False),
    sa.Column('element_ids_json', sa.JSON(), nullable=True),
    sa.Column('slice_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('locator_json', sa.JSON(), nullable=True),
    sa.Column('budget_json', sa.JSON(), nullable=True),
    sa.Column('receipt_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('provider_dispatch_group_id', sa.String(length=128), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('settled_at', sa.DateTime(), nullable=True),
    sa.CheckConstraint("status IN ('prepared', 'succeeded', 'failed', 'countermanded')", name='ck_turn_input_read_receipt_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'turn_id', 'receipt_checksum', name='uq_turn_read_receipt')
    )
    op.create_index(op.f('ix_turn_input_read_receipts_provider_dispatch_group_id'), 'turn_input_read_receipts', ['provider_dispatch_group_id'], unique=False)
    op.create_index(op.f('ix_turn_input_read_receipts_receipt_checksum'), 'turn_input_read_receipts', ['receipt_checksum'], unique=False)
    op.create_index(op.f('ix_turn_input_read_receipts_snapshot_id'), 'turn_input_read_receipts', ['snapshot_id'], unique=False)
    op.create_index(op.f('ix_turn_input_read_receipts_status'), 'turn_input_read_receipts', ['status'], unique=False)
    op.create_index(op.f('ix_turn_input_read_receipts_tenant_id'), 'turn_input_read_receipts', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_turn_input_read_receipts_turn_id'), 'turn_input_read_receipts', ['turn_id'], unique=False)
    op.create_table('turn_input_snapshots',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('turn_id', sa.String(length=128), nullable=False),
    sa.Column('session_id', sa.String(length=128), nullable=False),
    sa.Column('message_resource_link_id', sa.String(length=128), nullable=False),
    sa.Column('resource_id', sa.String(length=128), nullable=False),
    sa.Column('resource_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('extraction_id', sa.String(length=128), nullable=False),
    sa.Column('extraction_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('element_manifest_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('resource_acl_revision_at_snapshot', sa.Integer(), nullable=False),
    sa.Column('opaque_handle', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'opaque_handle', name='uq_turn_input_snapshot_handle'),
    sa.UniqueConstraint('tenant_id', 'turn_id', 'message_resource_link_id', name='uq_turn_input_snapshot_link')
    )
    op.create_index(op.f('ix_turn_input_snapshots_extraction_id'), 'turn_input_snapshots', ['extraction_id'], unique=False)
    op.create_index(op.f('ix_turn_input_snapshots_message_resource_link_id'), 'turn_input_snapshots', ['message_resource_link_id'], unique=False)
    op.create_index(op.f('ix_turn_input_snapshots_opaque_handle'), 'turn_input_snapshots', ['opaque_handle'], unique=False)
    op.create_index(op.f('ix_turn_input_snapshots_resource_id'), 'turn_input_snapshots', ['resource_id'], unique=False)
    op.create_index(op.f('ix_turn_input_snapshots_session_id'), 'turn_input_snapshots', ['session_id'], unique=False)
    op.create_index(op.f('ix_turn_input_snapshots_tenant_id'), 'turn_input_snapshots', ['tenant_id'], unique=False)
    op.add_column(
        "managed_input_resources",
        sa.Column("access_status", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False, server_default="active"),
    )
    op.add_column(
        "managed_input_resources",
        sa.Column(
            "security_status",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
            server_default="pending_scan",
        ),
    )
    op.add_column(
        "managed_input_resources",
        sa.Column(
            "destruction_status",
            sqlmodel.sql.sqltypes.AutoString(length=64),
            nullable=False,
            server_default="retained",
        ),
    )
    op.add_column(
        "managed_input_resources",
        sa.Column("upload_binding_id", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_managed_input_resources_access_status", "managed_input_resources", ["access_status"])
    op.create_index("ix_managed_input_resources_security_status", "managed_input_resources", ["security_status"])
    op.create_index(
        "ix_managed_input_resources_destruction_status",
        "managed_input_resources",
        ["destruction_status"],
    )
    op.create_index(
        "ix_managed_input_resources_upload_binding_id",
        "managed_input_resources",
        ["upload_binding_id"],
    )
    op.add_column("input_resource_snapshots", sa.Column("extraction_id", sa.String(length=512), nullable=True))
    op.add_column("input_resource_snapshots", sa.Column("opaque_handle", sqlmodel.sql.sqltypes.AutoString(length=96), nullable=True))
    op.add_column(
        "input_resource_snapshots",
        sa.Column("parser_name", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    )
    op.add_column(
        "input_resource_snapshots",
        sa.Column("parser_version", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    )
    op.add_column(
        "input_resource_snapshots",
        sa.Column("parser_config_checksum", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    )
    op.add_column(
        "input_resource_snapshots",
        sa.Column("element_manifest_checksum", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    )
    op.add_column(
        "input_resource_snapshots",
        sa.Column("resource_acl_revision_at_snapshot", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_input_resource_snapshots_extraction_id", "input_resource_snapshots", ["extraction_id"])
    op.create_index("ix_input_resource_snapshots_opaque_handle", "input_resource_snapshots", ["opaque_handle"])

    op.create_table('artifact_renderer_jobs',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
    sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('execution_id', sa.String(length=128), nullable=False),
    sa.Column('result_id', sa.String(length=128), nullable=False),
    sa.Column('result_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('source_node_execution_id', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('artifact_key', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('filename', sqlmodel.sql.sqltypes.AutoString(length=191), nullable=False),
    sa.Column('mime_type', sqlmodel.sql.sqltypes.AutoString(length=191), nullable=False),
    sa.Column('renderer_version', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('required', sa.Boolean(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('lease_owner', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
    sa.Column('fencing_token', sa.Integer(), nullable=False),
    sa.Column('staged_checksum', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('artifact_id', sa.String(length=512), nullable=True),
    sa.Column('error_code', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('retry_at', sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql'), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("status IN ('pending', 'claimed', 'rendering', 'staged', 'ready', 'retry_wait', 'failed', 'dead_letter', 'cancelled')", name='ck_artifact_renderer_job_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'execution_id', 'result_checksum', 'artifact_key', 'renderer_version', name='uq_artifact_renderer_job_identity')
    )
    op.create_index(op.f('ix_artifact_renderer_jobs_artifact_id'), 'artifact_renderer_jobs', ['artifact_id'], unique=False)
    op.create_index(op.f('ix_artifact_renderer_jobs_artifact_key'), 'artifact_renderer_jobs', ['artifact_key'], unique=False)
    op.create_index(op.f('ix_artifact_renderer_jobs_execution_id'), 'artifact_renderer_jobs', ['execution_id'], unique=False)
    op.create_index(op.f('ix_artifact_renderer_jobs_lease_expires_at'), 'artifact_renderer_jobs', ['lease_expires_at'], unique=False)
    op.create_index(op.f('ix_artifact_renderer_jobs_lease_owner'), 'artifact_renderer_jobs', ['lease_owner'], unique=False)
    op.create_index(op.f('ix_artifact_renderer_jobs_required'), 'artifact_renderer_jobs', ['required'], unique=False)
    op.create_index(op.f('ix_artifact_renderer_jobs_result_checksum'), 'artifact_renderer_jobs', ['result_checksum'], unique=False)
    op.create_index(op.f('ix_artifact_renderer_jobs_result_id'), 'artifact_renderer_jobs', ['result_id'], unique=False)
    op.create_index(op.f('ix_artifact_renderer_jobs_retry_at'), 'artifact_renderer_jobs', ['retry_at'], unique=False)
    op.create_index(op.f('ix_artifact_renderer_jobs_status'), 'artifact_renderer_jobs', ['status'], unique=False)
    op.create_index(op.f('ix_artifact_renderer_jobs_tenant_id'), 'artifact_renderer_jobs', ['tenant_id'], unique=False)


def downgrade() -> None:
    """仅在没有附件 A++ 业务事实时移除新增契约，避免静默丢失审计与文件血缘。"""

    bind = op.get_bind()
    protected_tables = (
        "artifact_renderer_jobs",
        "message_input_binding_links",
        "turn_input_read_receipts",
        "turn_input_snapshots",
        "provider_input_dispatch_receipts",
        "provider_input_dispatch_groups",
        "message_input_resource_links",
        "resource_session_bindings",
        "selected_resource_extractions",
        "input_document_elements",
        "input_resource_extractions",
        "input_resource_extraction_attempts",
        "scanner_evidence",
        "draft_upload_bindings",
    )
    for table_name in protected_tables:
        count = int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one())
        if count:
            raise RuntimeError(f"cannot downgrade with attachment facts in {table_name}")

    op.drop_index("ix_input_resource_snapshots_extraction_id", table_name="input_resource_snapshots")
    op.drop_index("ix_input_resource_snapshots_opaque_handle", table_name="input_resource_snapshots")
    for column in (
        "resource_acl_revision_at_snapshot",
        "element_manifest_checksum",
        "parser_config_checksum",
        "parser_version",
        "parser_name",
        "extraction_id",
        "opaque_handle",
    ):
        op.drop_column("input_resource_snapshots", column)
    op.drop_index("ix_managed_input_resources_destruction_status", table_name="managed_input_resources")
    op.drop_index("ix_managed_input_resources_upload_binding_id", table_name="managed_input_resources")
    op.drop_index("ix_managed_input_resources_security_status", table_name="managed_input_resources")
    op.drop_index("ix_managed_input_resources_access_status", table_name="managed_input_resources")
    for column in ("upload_binding_id", "destruction_status", "security_status", "access_status"):
        op.drop_column("managed_input_resources", column)
    for table_name in protected_tables:
        op.drop_table(table_name)
