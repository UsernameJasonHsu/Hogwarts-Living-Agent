"""M2.3.1 immutable evidence metadata and storage lifecycle."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "artifact_metadata",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("prototype_runs.id"), nullable=False),
        sa.Column(
            "workspace_id", sa.String(36), sa.ForeignKey("git_workspaces.id"), nullable=False
        ),
        sa.Column(
            "sandbox_id", sa.String(36), sa.ForeignKey("sandbox_instances.id"), nullable=False
        ),
        sa.Column("owner_id", sa.String(100), nullable=False),
        sa.Column("producer_id", sa.String(100), nullable=False),
        sa.Column("lease_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("metadata_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.UniqueConstraint("run_id", "producer_id", "idempotency_hash"),
        sa.CheckConstraint("state IN ('PENDING','READY','FAILED','CORRUPT')"),
        sa.CheckConstraint("schema_version >= 1"),
        sa.CheckConstraint("size_bytes >= 0 AND size_bytes <= 16777216"),
    )


def downgrade():
    op.drop_table("artifact_metadata")
