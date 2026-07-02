"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  SQLAlchemy Model Definitions (Declarative)                                 ║
║  ──────────────────────────────────────────────                             ║
║  Unified models for all three demos in this project.                        ║
║  Used by the migration tool to auto-detect schema changes.                  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from sqlalchemy import (
    ARRAY,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all migration-managed models."""
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


# ──────────────────────────────────────────────────────────────────────────────
# concurrency_demo.py models
# ──────────────────────────────────────────────────────────────────────────────

class Counter(Base):
    __tablename__ = "concurrency_counter"

    id      = Column(Integer, primary_key=True)
    label   = Column(Text, nullable=False)
    count   = Column(Integer, nullable=False, server_default="0")
    version = Column(Integer, nullable=False, server_default="1")


class User(Base):
    __tablename__ = "concurrency_users"

    id    = Column(Integer, primary_key=True)
    email = Column(Text, nullable=False)
    name  = Column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("email", name="uq_concurrency_users_email"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# demo.py models
# ──────────────────────────────────────────────────────────────────────────────

class Picklist(Base):
    __tablename__ = "picklist_demo"

    id          = Column(Integer, primary_key=True)
    code        = Column(Text, nullable=False, unique=True)
    status      = Column(Text, nullable=False, server_default="pending")
    codes_array = Column(ARRAY(Text), nullable=False, server_default="{}")


class Team(Base):
    __tablename__ = "team_demo"

    id   = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False, unique=True)


class PicklistTeam(Base):
    __tablename__ = "jt_picklist_team_demo"

    id          = Column(Integer, primary_key=True)
    picklist_id = Column(Integer, ForeignKey("picklist_demo.id"), nullable=False)
    team_id     = Column(Integer, ForeignKey("team_demo.id"), nullable=False)

    __table_args__ = (
        UniqueConstraint("picklist_id", "team_id", name="uq_picklist_team"),
    )


class PickItem(Base):
    __tablename__ = "pickitem_demo"

    id          = Column(Integer, primary_key=True)
    picklist_id = Column(Integer, ForeignKey("picklist_demo.id"), nullable=False)
    item_code   = Column(Text, nullable=False)
    needs_review = Column(Integer, nullable=False, server_default="0")
    status      = Column(Text, nullable=False, server_default="ok")


class AuditLog(Base):
    __tablename__ = "picklist_audit_demo"

    id          = Column(Integer, primary_key=True)
    picklist_id = Column(Integer, ForeignKey("picklist_demo.id"), nullable=False)
    action      = Column(Text, nullable=False)
    created_at  = Column(Text, nullable=False)


class DemoLog(Base):
    __tablename__ = "demo_log_inserter"

    id    = Column(Integer, primary_key=True)
    value = Column(Text, nullable=False)


# ──────────────────────────────────────────────────────────────────────────────
# pg_triggers_redis.py models
# ──────────────────────────────────────────────────────────────────────────────

class DemoOrder(Base):
    __tablename__ = "demo_orders"

    id         = Column(Integer, primary_key=True)
    customer   = Column(Text, nullable=False)
    product    = Column(Text, nullable=False)
    quantity   = Column(Integer, nullable=False, server_default="1")
    status     = Column(Text, nullable=False, server_default="pending")
    created_at = Column(Text, nullable=False, server_default="NOW()")
    updated_at = Column(Text, nullable=False, server_default="NOW()")


class DemoOrderAudit(Base):
    __tablename__ = "demo_orders_audit"

    id         = Column(Integer, primary_key=True)
    order_id   = Column(Integer, ForeignKey("demo_orders.id"), nullable=False)
    action     = Column(Text, nullable=False)
    old_data   = Column(Text, nullable=True)   # JSONB stored as Text for portability
    new_data   = Column(Text, nullable=True)   # JSONB stored as Text for portability
    changed_by = Column(Text, nullable=False, server_default="app")
    changed_at = Column(Text, nullable=False, server_default="NOW()")


# ── Registry for auto-detection ──────────────────────────────────────────────

# All models the migration tool should track
ALL_MODELS: list[type[Base]] = [
    Counter,
    User,
    Picklist,
    Team,
    PicklistTeam,
    PickItem,
    AuditLog,
    DemoLog,
    DemoOrder,
    DemoOrderAudit,
]
