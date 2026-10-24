"""M2.3.2 durable sealed output imports."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "output_imports",
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
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("seal_key", sa.String(64), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("archive_sha256", sa.String(64), nullable=True),
        sa.Column("proof", sa.JSON(), nullable=True),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column(
            "manifest_artifact_id",
            sa.String(36),
            sa.ForeignKey("artifact_metadata.id"),
            nullable=True,
        ),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sandbox_id"),
        sa.CheckConstraint(
            "state IN ('PENDING','SNAPSHOTTING','SNAPSHOT_READY','IMPORTING','IMPORTED',"
            "'REJECTED','FAILED','UNCERTAIN')"
        ),
    )


def downgrade():
    op.drop_table("output_imports")
