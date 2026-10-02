import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id.get(),
        }
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            data["stack_info"] = record.stack_info
        for field in (
            "user_id",
            "centre_id",
            "test_id",
            "booking_id",
            "payment_id",
            "provider_ref",
            "event_id",
            "attempts",
            "method",
            "path",
            "status",
            "duration_ms",
        ):
            if hasattr(record, field):
                data[field] = getattr(record, field)
        return json.dumps(data)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
