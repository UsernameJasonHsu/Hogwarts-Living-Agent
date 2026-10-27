"""Durable heartbeat and monotonic fencing; no resource reconciler."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    for c in [
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_issued_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("lease_revoked_at", sa.DateTime(timezone=True)),
        sa.Column("lease_revocation_reason", sa.String(50)),
        sa.Column("lease_stop_state", sa.String(20), nullable=False, server_default="NONE"),
    ]:
        op.add_column("prototype_runs", c)
    op.execute(
        "UPDATE prototype_runs SET lease_generation=1, lease_issued_generation=1, lease_version=1 "
        "WHERE lease_token IS NOT NULL"
    )


def downgrade():
    for name in [
        "lease_stop_state",
        "lease_revocation_reason",
        "lease_revoked_at",
        "lease_heartbeat_at",
        "lease_version",
        "lease_issued_generation",
        "lease_generation",
    ]:
        op.drop_column("prototype_runs", name)
