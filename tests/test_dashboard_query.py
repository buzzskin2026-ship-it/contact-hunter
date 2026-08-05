from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Contact


def test_dashboard_high_quality_count_query_executes() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        result = session.scalar(
            select(func.count(Contact.id)).where(Contact.reliability == "high")
        )

    assert result == 0
