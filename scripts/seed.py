import os
import sys
from decimal import Decimal

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert

from app.db import SessionLocal
from app.models import Centre, CentreTest, DiagnosticTest, User
from app.security import hash_password

CENTRES = [
    ("CityCare Diagnostics", "Andheri, Mumbai"),
    ("Kaveri Diagnostics", "Koramangala, Bengaluru"),
    ("Capital Path Labs", "Connaught Place, New Delhi"),
    ("Marina Diagnostics", "Adyar, Chennai"),
]

TESTS = [
    ("Complete Blood Count (CBC)", "Haemoglobin, RBC, WBC and platelets", "350.00"),
    ("HbA1c", "Three-month average blood sugar", "550.00"),
    ("Lipid Profile", "Cholesterol and triglycerides, fasting", "900.00"),
    ("Thyroid Panel", "T3, T4 and TSH", "750.00"),
    ("Vitamin D (25-OH)", "Vitamin D deficiency screening", "1500.00"),
]


def main() -> None:
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not email or not 8 <= len(password) <= 128:
        sys.exit("Set ADMIN_EMAIL and ADMIN_PASSWORD (8..128 chars) in the environment")

    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.email == email))
        if admin is None:
            db.add(
                User(
                    email=email,
                    password_hash=hash_password(password),
                    full_name="Admin",
                    is_admin=True,
                )
            )
        elif not admin.is_admin:
            admin.is_admin = True

        db.execute(
            insert(Centre)
            .values([{"name": name, "location": loc} for name, loc in CENTRES])
            .on_conflict_do_nothing(constraint="uq_centres_name_location")
        )
        db.execute(
            insert(DiagnosticTest)
            .values([{"name": name, "description": desc} for name, desc, _ in TESTS])
            .on_conflict_do_nothing(constraint="uq_diagnostic_tests_name")
        )
        db.flush()

        prices = {name: Decimal(price) for name, _, price in TESTS}
        centre_ids = db.scalars(
            select(Centre.id).where(tuple_(Centre.name, Centre.location).in_(CENTRES))
        ).all()
        test_ids = dict(
            db.execute(
                select(DiagnosticTest.name, DiagnosticTest.id).where(
                    DiagnosticTest.name.in_(list(prices))
                )
            ).all()
        )
        db.execute(
            insert(CentreTest)
            .values(
                [
                    {"centre_id": cid, "test_id": test_ids[name], "price": prices[name]}
                    for cid in centre_ids
                    for name in prices
                ]
            )
            .on_conflict_do_nothing(constraint="uq_centre_tests_centre_test")
        )
        db.commit()
    print(f"Seeded admin, {len(CENTRES)} centres, {len(TESTS)} tests; prices in INR")


if __name__ == "__main__":
    main()
