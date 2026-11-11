"""initial schema

Revision ID: 0001
Revises: -
Create Date: 2026-11-11 12:45:45.504964
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('approvals',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('execution_id', sa.String(length=40), nullable=True),
    sa.Column('task_id', sa.String(length=40), nullable=True),
    sa.Column('action', sa.String(length=128), nullable=False),
    sa.Column('category', sa.String(length=40), nullable=False),
    sa.Column('risk', sa.Integer(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('details', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('scope', sa.String(length=16), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_by', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_approvals'))
    )
    with op.batch_alter_table('approvals', schema=None) as batch_op:
        batch_op.create_index('ix_approvals_execution_id', ['execution_id'], unique=False)
        batch_op.create_index('ix_approvals_fingerprint', ['fingerprint'], unique=False)
        batch_op.create_index('ix_approvals_status', ['status'], unique=False)

    op.create_table('assets',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=False),
    sa.Column('scene_id', sa.String(length=64), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('checksum', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assets'))
    )
    with op.batch_alter_table('assets', schema=None) as batch_op:
        batch_op.create_index('ix_assets_project_id_kind', ['project_id', 'kind'], unique=False)

    op.create_table('cache_entries',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('value', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('hits', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_cache_entries'))
    )
    with op.batch_alter_table('cache_entries', schema=None) as batch_op:
        batch_op.create_index('ix_cache_entries_kind', ['kind'], unique=False)

    op.create_table('cost_records',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('execution_id', sa.String(length=40), nullable=True),
    sa.Column('project_id', sa.String(length=40), nullable=True),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=True),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('units', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('estimated_usd', sa.Float(), nullable=False),
    sa.Column('actual_usd', sa.Float(), nullable=True),
    sa.Column('priced', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cost_records'))
    )
    with op.batch_alter_table('cost_records', schema=None) as batch_op:
        batch_op.create_index('ix_cost_records_created_at', ['created_at'], unique=False)
        batch_op.create_index('ix_cost_records_execution_id', ['execution_id'], unique=False)
        batch_op.create_index('ix_cost_records_project_id', ['project_id'], unique=False)

    op.create_table('creative_projects',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('brief', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('root_dir', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_creative_projects'))
    )
    with op.batch_alter_table('creative_projects', schema=None) as batch_op:
        batch_op.create_index('ix_creative_projects_created_at', ['created_at'], unique=False)
        batch_op.create_index('ix_creative_projects_status', ['status'], unique=False)

    op.create_table('executions',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('command', sa.Text(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('intent', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('result', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('project_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_executions'))
    )
    with op.batch_alter_table('executions', schema=None) as batch_op:
        batch_op.create_index('ix_executions_created_at', ['created_at'], unique=False)
        batch_op.create_index('ix_executions_project_id', ['project_id'], unique=False)
        batch_op.create_index('ix_executions_status', ['status'], unique=False)

    op.create_table('generations',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=True),
    sa.Column('execution_id', sa.String(length=40), nullable=True),
    sa.Column('scene_id', sa.String(length=64), nullable=True),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=True),
    sa.Column('model', sa.String(length=128), nullable=True),
    sa.Column('prompt', sa.Text(), nullable=True),
    sa.Column('params', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('cache_key', sa.String(length=64), nullable=True),
    sa.Column('output_path', sa.Text(), nullable=True),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('qc', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('selected', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_generations'))
    )
    with op.batch_alter_table('generations', schema=None) as batch_op:
        batch_op.create_index('ix_generations_cache_key', ['cache_key'], unique=False)
        batch_op.create_index('ix_generations_created_at', ['created_at'], unique=False)
        batch_op.create_index('ix_generations_execution_id', ['execution_id'], unique=False)
        batch_op.create_index('ix_generations_project_id_status', ['project_id', 'status'], unique=False)
        batch_op.create_index('ix_generations_status', ['status'], unique=False)

    op.create_table('memories',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('scope', sa.String(length=64), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('tags', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('embedding', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('embedding_model', sa.String(length=64), nullable=True),
    sa.Column('importance', sa.Float(), nullable=False),
    sa.Column('access_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_accessed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_memories'))
    )
    with op.batch_alter_table('memories', schema=None) as batch_op:
        batch_op.create_index('ix_memories_created_at', ['created_at'], unique=False)
        batch_op.create_index('ix_memories_kind', ['kind'], unique=False)
        batch_op.create_index('ix_memories_scope', ['scope'], unique=False)

    op.create_table('scheduled_jobs',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('command', sa.Text(), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('expression', sa.String(length=120), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_status', sa.String(length=24), nullable=True),
    sa.Column('last_execution_id', sa.String(length=40), nullable=True),
    sa.Column('failure_count', sa.Integer(), nullable=False),
    sa.Column('run_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scheduled_jobs')),
    sa.UniqueConstraint('name', name=op.f('uq_scheduled_jobs_name'))
    )
    with op.batch_alter_table('scheduled_jobs', schema=None) as batch_op:
        batch_op.create_index('ix_scheduled_jobs_status', ['status'], unique=False)

    op.create_table('task_events',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('execution_id', sa.String(length=40), nullable=False),
    sa.Column('task_id', sa.String(length=40), nullable=True),
    sa.Column('type', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_task_events'))
    )
    with op.batch_alter_table('task_events', schema=None) as batch_op:
        batch_op.create_index('ix_task_events_execution_id_id', ['execution_id', 'id'], unique=False)

    op.create_table('tasks',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('execution_id', sa.String(length=40), nullable=False),
    sa.Column('parent_id', sa.String(length=40), nullable=True),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('agent', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('dependencies', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('input', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('output', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('max_retries', sa.Integer(), nullable=False),
    sa.Column('timeout', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tasks'))
    )
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.create_index('ix_tasks_created_at', ['created_at'], unique=False)
        batch_op.create_index('ix_tasks_execution_id_status', ['execution_id', 'status'], unique=False)
        batch_op.create_index('ix_tasks_status', ['status'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_index('ix_tasks_status')
        batch_op.drop_index('ix_tasks_execution_id_status')
        batch_op.drop_index('ix_tasks_created_at')

    op.drop_table('tasks')
    with op.batch_alter_table('task_events', schema=None) as batch_op:
        batch_op.drop_index('ix_task_events_execution_id_id')

    op.drop_table('task_events')
    with op.batch_alter_table('scheduled_jobs', schema=None) as batch_op:
        batch_op.drop_index('ix_scheduled_jobs_status')

    op.drop_table('scheduled_jobs')
    with op.batch_alter_table('memories', schema=None) as batch_op:
        batch_op.drop_index('ix_memories_scope')
        batch_op.drop_index('ix_memories_kind')
        batch_op.drop_index('ix_memories_created_at')

    op.drop_table('memories')
    with op.batch_alter_table('generations', schema=None) as batch_op:
        batch_op.drop_index('ix_generations_status')
        batch_op.drop_index('ix_generations_project_id_status')
        batch_op.drop_index('ix_generations_execution_id')
        batch_op.drop_index('ix_generations_created_at')
        batch_op.drop_index('ix_generations_cache_key')

    op.drop_table('generations')
    with op.batch_alter_table('executions', schema=None) as batch_op:
        batch_op.drop_index('ix_executions_status')
        batch_op.drop_index('ix_executions_project_id')
        batch_op.drop_index('ix_executions_created_at')

    op.drop_table('executions')
    with op.batch_alter_table('creative_projects', schema=None) as batch_op:
        batch_op.drop_index('ix_creative_projects_status')
        batch_op.drop_index('ix_creative_projects_created_at')

    op.drop_table('creative_projects')
    with op.batch_alter_table('cost_records', schema=None) as batch_op:
        batch_op.drop_index('ix_cost_records_project_id')
        batch_op.drop_index('ix_cost_records_execution_id')
        batch_op.drop_index('ix_cost_records_created_at')

    op.drop_table('cost_records')
    with op.batch_alter_table('cache_entries', schema=None) as batch_op:
        batch_op.drop_index('ix_cache_entries_kind')

    op.drop_table('cache_entries')
    with op.batch_alter_table('assets', schema=None) as batch_op:
        batch_op.drop_index('ix_assets_project_id_kind')

    op.drop_table('assets')
    with op.batch_alter_table('approvals', schema=None) as batch_op:
        batch_op.drop_index('ix_approvals_status')
        batch_op.drop_index('ix_approvals_fingerprint')
        batch_op.drop_index('ix_approvals_execution_id')

    op.drop_table('approvals')
