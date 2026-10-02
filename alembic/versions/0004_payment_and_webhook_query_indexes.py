"""Index payment attempt counts and failed webhook claims."""

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index("ix_payments_booking_id", "payments", ["booking_id"])
    op.create_index("ix_webhook_events_status_id", "webhook_events", ["status", "id"])


def downgrade() -> None:
    op.drop_index("ix_webhook_events_status_id", table_name="webhook_events")
    op.drop_index("ix_payments_booking_id", table_name="payments")
