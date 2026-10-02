import os
from collections.abc import Callable, Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://eve:eve@localhost:5433/eve")
os.environ["JWT_SECRET"] = "test-only-jwt-secret-at-least-32-characters"
os.environ["WEBHOOK_SECRET"] = "test-only-webhook-secret-0123456789"

from app.db import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base, Centre, CentreTest, DiagnosticTest, User  # noqa: E402
from app.ratelimit import reset_rate_limits  # noqa: E402
from app.security import create_access_token, hash_password  # noqa: E402


@pytest.fixture(autouse=True)
def reset_request_limits() -> Iterator[None]:
    reset_rate_limits()
    yield
    reset_rate_limits()


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.fail("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database")
    try:
        url = make_url(database_url)
    except (ArgumentError, ValueError):
        pytest.fail("TEST_DATABASE_URL must be a valid PostgreSQL URL")
    if (
        url.drivername != "postgresql+psycopg"
        or not (url.database or "").startswith("eve_test")
        or url.query
    ):
        pytest.fail(
            "Test cleanup requires an eve_test* PostgreSQL database "
            "without URL query parameters"
        )
    engine = create_engine(database_url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            assert_test_database(connection)
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()


def assert_test_database(connection: Connection) -> None:
    database = connection.scalar(text("SELECT current_database()"))
    if not isinstance(database, str) or not database.startswith("eve_test"):
        pytest.fail(
            "Connected database must start with eve_test before migration or cleanup"
        )


@pytest.fixture
def db(test_engine: Engine) -> Iterator[Session]:
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with test_engine.begin() as connection:
        assert_test_database(connection)
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    with Session(test_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    application = create_app()

    def override_db() -> Iterator[Session]:
        with Session(db.get_bind(), expire_on_commit=False) as session:
            yield session

    application.dependency_overrides[get_db] = override_db
    with TestClient(application, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def make_user(db: Session) -> Callable[..., User]:
    def create(email: str | None = None, is_admin: bool = False) -> User:
        user = User(
            email=email or f"user-{uuid4().hex}@example.com",
            password_hash=hash_password("test-password"),
            full_name="Test User",
            is_admin=is_admin,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    return create


@pytest.fixture
def auth_headers() -> Callable[[User], dict[str, str]]:
    def headers(user: User) -> dict[str, str]:
        return {"Authorization": f"Bearer {create_access_token(user.id)}"}

    return headers


@pytest.fixture
def make_offering(db: Session) -> Callable[..., CentreTest]:
    def create(price: Decimal = Decimal("500.00")) -> CentreTest:
        suffix = uuid4().hex
        centre = Centre(name=f"Centre {suffix}", location="Test City")
        test = DiagnosticTest(
            name=f"Test {suffix}", description="Sample diagnostic test"
        )
        db.add_all([centre, test])
        db.flush()
        offering = CentreTest(centre_id=centre.id, test_id=test.id, price=price)
        db.add(offering)
        db.commit()
        db.refresh(offering)
        return offering

    return create
