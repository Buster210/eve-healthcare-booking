"""Index webhook_events.provider_ref for provider reconciliation lookups.

No events are deleted. Event IDs preserve replay deduplication; any future
retention policy must account for the provider's maximum retry window.
"""

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_webhook_events_provider_ref",
        "webhook_events",
        ["provider_ref"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_events_provider_ref", table_name="webhook_events")
