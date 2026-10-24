"""M2.2 durable Linux sandbox lifecycle."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sandbox_instances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("prototype_runs.id"), nullable=False, unique=True
        ),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("git_workspaces.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("owner_id", sa.String(100), nullable=False),
        sa.Column("lease_hash", sa.String(64), nullable=False),
        sa.Column("profile_name", sa.String(40), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("container_id", sa.String(64)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("lifetime_seconds", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invocation_id", sa.String(64)),
        sa.Column("result", sa.JSON()),
        sa.CheckConstraint(
            "state IN ('CREATING','CREATED','RUNNING','EXECUTING','COMPLETED',"
            "'FAILED','STOPPED','DESTROYED','CLEANUP_FAILED')"
        ),
    )


def downgrade():
    op.drop_table("sandbox_instances")
