"""Database models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AssetGroup(Base):
    __tablename__ = "asset_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    metadata_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    assets: Mapped[list[Asset]] = relationship(back_populates="group")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(100), index=True)
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("asset_groups.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    metadata_values: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict
    )
    file_name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    image_data: Mapped[bytes] = mapped_column(LargeBinary)
    print_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # A BMP contains only final pixels, so retain the controls used to create it too.
    print_adjustment: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    favorite: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(30), default="user_upload")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    group: Mapped[AssetGroup | None] = relationship(back_populates="assets")


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    label_width_mm: Mapped[float]
    label_height_mm: Mapped[float]
    printer_dpi: Mapped[int] = mapped_column(Integer, default=203)
    margins: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    input_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Printer(Base):
    """A server-side TSPL destination selected by an operator."""

    __tablename__ = "printers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    transport: Mapped[str] = mapped_column(String(20))
    device_uri: Mapped[str] = mapped_column(String(500), unique=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
