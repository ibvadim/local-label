"""Request payload models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

class MetadataAttribute(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    type: Literal["string", "number", "enum", "boolean"]
    unit: str | None = Field(default=None, max_length=30)
    required: bool = False
    enum_values: list[str] = Field(default_factory=list, max_length=100)


class AssetGroupPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    metadata_schema: list[MetadataAttribute] = Field(
        default_factory=list, max_length=50
    )


class AssetUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    group_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImageAdjustment(BaseModel):
    brightness: float = Field(default=1, ge=0.2, le=3)
    contrast: float = Field(default=1.5, ge=0.2, le=3)
    sharpness: float = Field(default=1, ge=0, le=3)
    # Zero selects Pillow's dithering conversion.
    threshold: int = Field(default=0, ge=0, le=255)
    invert: bool = False
    rotation: Literal[0, 90, 180, 270] = 0
    size: int = Field(default=512, ge=32, le=1024)


def default_print_adjustment() -> ImageAdjustment:
    """The same initial settings shown by the icon BMP editor."""
    return ImageAdjustment()


class TemplateFieldPayload(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    type: Literal["text", "qr", "barcode", "image", "bar", "box", "circle"]
    # Positions sent by current editors are printer-head dots.  The mm fields
    # remain accepted so saved templates from older versions keep working.
    x_mm: float = Field(default=0, ge=0)
    y_mm: float = Field(default=0, ge=0)
    w_mm: float = Field(default=1, gt=0)
    h_mm: float = Field(default=1, gt=0)
    x_dots: int | None = Field(default=None, ge=0)
    y_dots: int | None = Field(default=None, ge=0)
    w_dots: int | None = Field(default=None, gt=0)
    h_dots: int | None = Field(default=None, gt=0)
    rotation: float = Field(default=0, ge=0, lt=360)
    binding: Literal["static", "input", "asset_ref", "derived"] = "static"
    default_value: str = Field(default="", max_length=1000)
    value_template: str = Field(default="", max_length=1000)
    input_label: str = Field(default="", max_length=100)
    input_kind: Literal["text", "enum"] = "text"
    enum_values: list[str] = Field(default_factory=list, max_length=100)
    asset_id: str | None = Field(default=None, max_length=32)
    fit_mode: Literal["contain", "cover", "stretch"] = "contain"
    restrict_group_id: str | None = Field(default=None, max_length=32)
    source_field_id: str | None = Field(default=None, max_length=64)
    metadata_key: str | None = Field(default=None, max_length=64)
    font_size_mm: float = Field(default=3, gt=0, le=30)
    tspl_font: str = Field(default="3", min_length=1, max_length=100)
    # Fonts 1–8 use integer raster multipliers (validated below).  Font 0
    # interprets these as point dimensions and therefore needs a wider range.
    tspl_x_mul: int = Field(default=1, ge=1, le=999)
    tspl_y_mul: int = Field(default=1, ge=1, le=999)
    text_align: Literal["left", "center", "right"] = "center"
    vertical_align: Literal["top", "middle", "bottom"] = "middle"
    wrap_text: bool = False
    line_thickness: int = Field(default=1, ge=1, le=999)
    corner_radius: int = Field(default=0, ge=0, le=999)


class TemplateUserInputPayload(BaseModel):
    """A value entered once in preview and reusable by text templates."""

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=100)
    type: Literal["text", "number", "enum"] = "text"
    default_value: str = Field(default="", max_length=1000)
    enum_values: list[str] = Field(default_factory=list, max_length=100)


class TemplatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    label_width_mm: float = Field(gt=0, le=500)
    label_height_mm: float = Field(gt=0, le=500)
    printer_dpi: int = Field(default=203, ge=72, le=1200)
    margins: dict[str, int] = Field(default_factory=dict)
    fields: list[TemplateFieldPayload] = Field(default_factory=list, max_length=100)
    input_fields: list[TemplateUserInputPayload] = Field(default_factory=list, max_length=100)


class TemplatePreviewPayload(BaseModel):
    """Values entered for a single, not-yet-printed label."""

    values: dict[str, str] = Field(default_factory=dict, max_length=100)


class TemplatePrintPayload(TemplatePreviewPayload):
    """Values used to build a native TSPL label program."""

    copies: int = Field(default=1, ge=1, le=999)
    printer_settings: dict[str, Any] = Field(default_factory=dict)


class PrinterSettingsPayload(BaseModel):
    gap_mm: float = Field(default=2, ge=0, le=100)
    gap_offset_mm: float = Field(default=0, ge=-100, le=100)
    speed_ips: float | None = Field(default=None, ge=0.1, le=12)
    density: int | None = Field(default=None, ge=0, le=15)
    direction: Literal[0, 1] = 1
    codepage: str = Field(default="UTF-8", min_length=1, max_length=30)


class PrinterCreatePayload(BaseModel):
    device_uri: str = Field(min_length=1, max_length=500)
    name: str = Field(default="", max_length=100)
    settings: PrinterSettingsPayload = Field(default_factory=PrinterSettingsPayload)


class PrinterUpdatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    settings: PrinterSettingsPayload
    is_active: bool = False


class ServerPrintPayload(TemplatePrintPayload):
    printer_id: str = Field(min_length=1, max_length=32)
