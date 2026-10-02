"""Track webhook processing and enforce positive finite amounts."""

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "webhook_events",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("webhook_events", sa.Column("last_error", sa.Text(), nullable=True))
    op.execute("UPDATE webhook_events SET status = 'PROCESSED'")
    op.alter_column("webhook_events", "status", server_default="PROCESSED")
    op.create_check_constraint(
        op.f("ck_webhook_events_status"),
        "webhook_events",
        "status IN ('PROCESSED', 'FAILED')",
    )
    op.drop_constraint(
        op.f("ck_centre_tests_price_positive"), "centre_tests", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_centre_tests_price_positive"),
        "centre_tests",
        "price > 0 AND price <> 'NaN'",
    )
    for table in ("bookings", "payments"):
        op.create_check_constraint(
            op.f(f"ck_{table}_amount_positive"),
            table,
            "amount > 0 AND amount <> 'NaN'",
        )


def downgrade() -> None:
    op.execute(sa.text("LOCK TABLE webhook_events IN SHARE ROW EXCLUSIVE MODE"))
    failed = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM webhook_events WHERE status = 'FAILED'"))
        .scalar()
    )
    if failed:
        raise RuntimeError(
            f"Cannot downgrade: {failed} webhook_events row(s) "
            "have status FAILED; retry/resolve them first"
        )
    for table in ("bookings", "payments"):
        op.drop_constraint(op.f(f"ck_{table}_amount_positive"), table, type_="check")
    op.drop_constraint(
        op.f("ck_centre_tests_price_positive"), "centre_tests", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_centre_tests_price_positive"), "centre_tests", "price > 0"
    )
    op.drop_constraint(
        op.f("ck_webhook_events_status"), "webhook_events", type_="check"
    )
    op.execute("UPDATE webhook_events SET status = payload->>'status'")
    op.alter_column("webhook_events", "status", server_default=None)
    op.drop_column("webhook_events", "last_error")
    op.drop_column("webhook_events", "attempts")
