from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from opp_ci.config import DATABASE_URL

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
