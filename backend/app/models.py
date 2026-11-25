from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    email = Column(String(128), unique=True, index=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(32), default="user", nullable=False)  # "admin" or "user"
    is_active = Column(Boolean, default=True, nullable=False)
    token_version = Column(Integer, default=1, nullable=False)
    vault_pin_hash = Column(String(256), nullable=True)
    email_verified_at = Column(DateTime, nullable=True)
    verification_token_hash = Column(String(128), index=True, nullable=True)
    verification_expires_at = Column(DateTime, nullable=True)
    reset_token_hash = Column(String(128), index=True, nullable=True)
    reset_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    passkeys = relationship(
        "UserPasskey",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    playback_progress = relationship(
        "PlaybackProgress",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class UserPasskey(Base):
    __tablename__ = "user_passkeys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    credential_id = Column(String(512), unique=True, index=True, nullable=False)
    public_key = Column(Text, nullable=False)
    sign_count = Column(Integer, default=0, nullable=False)
    transports = Column(String(256), default="", nullable=False)
    device_name = Column(String(128), default="Passkey", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="passkeys")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    username = Column(String(64), index=True, nullable=True)
    event = Column(String(64), index=True, nullable=False)
    detail = Column(Text, default="", nullable=False)
    ip_address = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class GrabHistory(Base):
    __tablename__ = "grab_history"

    id = Column(Integer, primary_key=True, index=True)
    info_hash = Column(String(128), index=True, nullable=True)
    title = Column(String(512), nullable=False)
    category = Column(String(64), default="Movies", nullable=False)
    indexer = Column(String(128), default="Unknown", nullable=False)
    size = Column(BigInteger, default=0, nullable=False)
    target = Column(String(64), default="qbittorrent", nullable=False)  # qbittorrent, radarr, sonarr, whisparr
    status = Column(String(64), default="grabbed", nullable=False)      # grabbed, downloading, completed
    created_at = Column(DateTime, default=utcnow, nullable=False)


class SearchLog(Base):
    __tablename__ = "search_logs"

    id = Column(Integer, primary_key=True, index=True)
    query = Column(String(512), nullable=False)
    category = Column(String(64), default="All", nullable=False)
    parsed_filters = Column(Text, default="{}", nullable=False)  # JSON string of extracted filters
    ai_used = Column(Boolean, default=False, nullable=False)
    result_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class EncryptedSetting(Base):
    __tablename__ = "encrypted_settings"

    key = Column(String(128), primary_key=True, index=True)
    encrypted_value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class PlaybackProgress(Base):
    __tablename__ = "playback_progress"
    __table_args__ = (UniqueConstraint("user_id", "item_id", name="uq_playback_user_item"),)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    item_id = Column(String(128), index=True, nullable=False)
    title = Column(String(512), default="", nullable=False)
    position_seconds = Column(Float, default=0.0, nullable=False)
    duration_seconds = Column(Float, default=0.0, nullable=False)
    completed = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    user = relationship("User", back_populates="playback_progress")

