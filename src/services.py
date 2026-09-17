"""Domain services: validation, migration, image processing, and TSPL generation."""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from .config import (
    ALLOWED_EXTENSIONS,
    LEGACY_GROUPS_FILE,
    LEGACY_ICONS_DIR,
    LEGACY_STATE_FILE,
    MAX_UPLOAD_BYTES,
    METADATA_KEY_RE,
)
from .database import SessionLocal, engine
from .models import Asset, AssetGroup, Template
from .schemas import (
    ImageAdjustment,
    MetadataAttribute,
    TemplatePayload,
    TemplateUserInputPayload,
    default_print_adjustment,
)

INPUT_TOKEN_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_-]{0,63})\}")
IMAGE_TOKEN_RE = re.compile(
    r"\{image:([A-Za-z][A-Za-z0-9_-]{0,63}):(title|[a-z][a-z0-9_]*)\}"
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def migrate_schema() -> None:
    """Apply the small additive migration needed by databases created earlier."""
    columns = {column["name"] for column in inspect(engine).get_columns("assets")}
    template_columns = {
        column["name"] for column in inspect(engine).get_columns("templates")
    }
    if "print_adjustment" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE assets ADD COLUMN print_adjustment JSON")
            )
    if "margins" not in template_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE templates ADD COLUMN margins JSON"))
    if "input_fields" not in template_columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE templates ADD COLUMN input_fields JSON")
            )


def safe_text(value: str, fallback: str, limit: int) -> str:
    return re.sub(r"\s+", " ", value.strip())[:limit] or fallback


def safe_title(value: str, fallback: str) -> str:
    return safe_text(value, fallback, 100)


def get_db() -> Session:
    with SessionLocal() as session:
        yield session


def legacy_json(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text("utf-8"))
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def legacy_image_path(item: dict[str, Any]) -> Path | None:
    relative = item.get("path")
    if relative:
        candidate = (LEGACY_ICONS_DIR / relative).resolve()
        if LEGACY_ICONS_DIR.resolve() in candidate.parents and candidate.is_file():
            return candidate
    candidate = (
        LEGACY_ICONS_DIR / str(item.get("id", "")) / str(item.get("filename", ""))
    )
    return candidate if candidate.is_file() else None


def migrate_legacy_library() -> None:
    """Import the previous JSON/file library once; source files remain a backup."""
    with SessionLocal.begin() as db:
        if db.scalar(select(func.count()).select_from(Asset)):
            return
        legacy_groups = legacy_json(LEGACY_GROUPS_FILE)
        group_id_map: dict[str, str] = {}
        names: dict[str, str] = {}
        for item in legacy_groups:
            if not item.get("id") or not item.get("name"):
                continue
            old_id = str(item["id"])
            name = safe_text(str(item["name"]), "Группа", 100)
            existing_id = names.get(name.casefold())
            if existing_id:
                group_id_map[old_id] = existing_id
                continue
            existing_group = db.get(AssetGroup, old_id) or db.scalar(
                select(AssetGroup).where(func.lower(AssetGroup.name) == name.casefold())
            )
            if existing_group:
                group_id_map[old_id] = existing_group.id
                names[name.casefold()] = existing_group.id
                continue
            group_id_map[old_id] = old_id
            names[name.casefold()] = old_id
            db.add(
                AssetGroup(
                    id=old_id,
                    name=name,
                    metadata_schema=item.get("metadata_schema", []),
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
        for item in legacy_json(LEGACY_STATE_FILE):
            path = legacy_image_path(item)
            if not path:
                continue
            try:
                data = path.read_bytes()
                image = read_image(data)
            except HTTPException:
                continue
            group_id = group_id_map.get(str(item.get("group_id")))
            asset_id = str(item.get("id") or uuid.uuid4().hex)
            db.add(
                Asset(
                    id=asset_id,
                    title=safe_title(str(item.get("title", "")), path.stem),
                    group_id=group_id,
                    metadata_values=item.get("metadata", {}) if group_id else {},
                    file_name=path.name,
                    mime_type=media_type(path.name),
                    image_data=data,
                    print_data=path.with_suffix(".bmp").read_bytes()
                    if path.with_suffix(".bmp").is_file()
                    else None,
                    width=image.width,
                    height=image.height,
                    favorite=bool(item.get("favorite", False)),
                    source="legacy_import",
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
            )


def normalized_schema(schema: list[MetadataAttribute]) -> list[dict[str, Any]]:
    keys: set[str] = set()
    result: list[dict[str, Any]] = []
    for attribute in schema:
        key = attribute.key.strip()
        if not METADATA_KEY_RE.fullmatch(key):
            raise HTTPException(
                422,
                "Ключ metadata: строчные латинские буквы, цифры и _, начиная с буквы",
            )
        if key in keys:
            raise HTTPException(422, f"Ключ metadata '{key}' повторяется")
        keys.add(key)
        values = [value.strip() for value in attribute.enum_values if value.strip()]
        if attribute.type == "enum" and not values:
            raise HTTPException(422, f"Для enum '{key}' укажите хотя бы один вариант")
        if attribute.type != "enum" and values:
            raise HTTPException(422, f"Варианты допустимы только для enum '{key}'")
        result.append(
            {
                **attribute.model_dump(),
                "key": key,
                "label": safe_text(attribute.label, key, 100),
                "unit": attribute.unit.strip() if attribute.unit else None,
                "enum_values": values,
            }
        )
    return result


def group_or_404(db: Session, group_id: str) -> AssetGroup:
    group = db.get(AssetGroup, group_id)
    if not group:
        raise HTTPException(404, "Группа не найдена")
    return group


def asset_or_404(db: Session, asset_id: str) -> Asset:
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "Иконка не найдена")
    return asset


def template_or_404(db: Session, template_id: str) -> Template:
    template = db.get(Template, template_id)
    if not template:
        raise HTTPException(404, "Шаблон не найден")
    return template


def validate_metadata(
    db: Session,
    group_id: str | None,
    metadata: dict[str, Any],
    group: AssetGroup | None = None,
) -> dict[str, Any]:
    if not group_id:
        if metadata:
            raise HTTPException(422, "Metadata можно задать только для иконки в группе")
        return {}
    group = group or group_or_404(db, group_id)
    schema = {attribute["key"]: attribute for attribute in group.metadata_schema}
    unknown = set(metadata) - set(schema)
    if unknown:
        raise HTTPException(422, f"Неизвестные атрибуты: {', '.join(sorted(unknown))}")
    result: dict[str, Any] = {}
    for key, attribute in schema.items():
        value = metadata.get(key)
        if value is None or value == "":
            if attribute["required"]:
                raise HTTPException(422, f"Атрибут '{attribute['label']}' обязателен")
            continue
        kind = attribute["type"]
        if kind == "string":
            if not isinstance(value, str):
                raise HTTPException(422, f"'{attribute['label']}' должен быть строкой")
            result[key] = value.strip()
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(422, f"'{attribute['label']}' должен быть числом")
            result[key] = value
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise HTTPException(422, f"'{attribute['label']}' должен быть boolean")
            result[key] = value
        elif value not in attribute["enum_values"]:
            raise HTTPException(
                422, f"Недопустимое значение '{value}' для '{attribute['label']}'"
            )
        else:
            result[key] = value
    return result


def read_image(data: bytes) -> Image.Image:
    if not data:
        raise HTTPException(422, "Выберите файл изображения")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Файл больше 15 МБ")
    try:
        image = Image.open(io.BytesIO(data))
        image.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
        return image
    except Exception as error:
        raise HTTPException(422, "Файл не является корректным изображением") from error


def media_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def parse_json_object(value: str, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise HTTPException(422, f"{label} должна быть JSON-объектом") from error
    if not isinstance(decoded, dict):
        raise HTTPException(422, f"{label} должна быть JSON-объектом")
    return decoded


def public_group(db: Session, group: AssetGroup) -> dict[str, Any]:
    count = (
        db.scalar(
            select(func.count()).select_from(Asset).where(Asset.group_id == group.id)
        )
        or 0
    )
    return {
        "id": group.id,
        "name": group.name,
        "metadata_schema": group.metadata_schema,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
        "asset_count": count,
    }


def public_asset(asset: Asset) -> dict[str, Any]:
    group = asset.group
    return {
        "id": asset.id,
        "title": asset.title,
        "group_id": asset.group_id,
        "metadata": asset.metadata_values,
        "file_name": asset.file_name,
        "mime_type": asset.mime_type,
        "width": asset.width,
        "height": asset.height,
        "favorite": asset.favorite,
        "source": asset.source,
        "created_at": asset.created_at,
        "updated_at": asset.updated_at,
        "has_print_data": asset.print_data is not None,
        "print_adjustment": asset.print_adjustment,
        "group": {"id": group.id, "name": group.name} if group else None,
        "image_url": f"/api/icons/{asset.id}/image",
        # Template canvases use this URL too, so their visual preview matches print.
        "print_image_url": f"/api/icons/{asset.id}/print-image",
    }


def public_template(template: Template) -> dict[str, Any]:
    return {
        "id": template.id,
        "name": template.name,
        "label_width_mm": template.label_width_mm,
        "label_height_mm": template.label_height_mm,
        "printer_dpi": template.printer_dpi,
        "margins": template.margins or {},
        "fields": template.fields,
        "input_fields": template.input_fields or [],
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


def normalized_margins(payload: TemplatePayload) -> dict[str, int]:
    """Return a valid dot-based printable area inset for editor operations."""
    margins = payload.margins or {}
    result = {
        side: int(margins.get(side, 0)) for side in ("top", "right", "bottom", "left")
    }
    if any(value < 0 for value in result.values()):
        raise HTTPException(422, "Поля этикетки не могут быть отрицательными")
    if result["left"] + result["right"] >= mm_to_px(
        payload.label_width_mm, payload.printer_dpi
    ):
        raise HTTPException(422, "Горизонтальные поля не оставляют рабочей области")
    if result["top"] + result["bottom"] >= mm_to_px(
        payload.label_height_mm, payload.printer_dpi
    ):
        raise HTTPException(422, "Вертикальные поля не оставляют рабочей области")
    return result


def validate_template_input_fields(
    input_fields: list[TemplateUserInputPayload],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    ids: set[str] = set()
    for payload in input_fields:
        item = payload.model_dump()
        field_id = item["id"].strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", field_id):
            raise HTTPException(
                422, "ID поля ввода: латинские буквы, цифры, _ или -, начиная с буквы"
            )
        if field_id in ids:
            raise HTTPException(422, f"ID поля ввода '{field_id}' повторяется")
        ids.add(field_id)
        values = [value.strip() for value in item["enum_values"] if value.strip()]
        if item["type"] == "enum":
            if not values:
                raise HTTPException(
                    422, f"Для enum-поля ввода '{field_id}' укажите варианты"
                )
            if len(set(values)) != len(values):
                raise HTTPException(
                    422, f"Варианты enum-поля ввода '{field_id}' повторяются"
                )
            if item["default_value"] and item["default_value"] not in values:
                raise HTTPException(
                    422, f"Значение по умолчанию '{field_id}' отсутствует в вариантах"
                )
        elif values:
            raise HTTPException(
                422, f"Варианты допустимы только для enum-поля '{field_id}'"
            )
        item["id"] = field_id
        item["label"] = safe_text(item["label"], field_id, 100)
        item["enum_values"] = values
        result.append(item)
    return result


def validate_template_fields(
    db: Session, payload: TemplatePayload, input_fields: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ids: set[str] = set()
    fields = [item.model_dump() for item in payload.fields]
    input_ids = {item["id"] for item in input_fields}
    label_width_dots = mm_to_px(payload.label_width_mm, payload.printer_dpi)
    label_height_dots = mm_to_px(payload.label_height_mm, payload.printer_dpi)
    for item in fields:
        # Canonical persisted coordinate system: thermal-head dots.  Convert
        # legacy mm payloads once at the API boundary, never while rendering.
        for axis in ("x", "y", "w", "h"):
            dots_key, mm_key = f"{axis}_dots", f"{axis}_mm"
            if item[dots_key] is None:
                item[dots_key] = (
                    mm_to_px(item[mm_key], payload.printer_dpi)
                    if axis in {"w", "h"}
                    else round(item[mm_key] / 25.4 * payload.printer_dpi)
                )
        if item["tspl_font"] in TSPL_FONT_CELLS:
            if item["tspl_x_mul"] > 10 or item["tspl_y_mul"] > 10:
                raise HTTPException(
                    422, "Множители встроенных TSPL-шрифтов: от 1 до 10"
                )
        field_id = item["id"].strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", field_id):
            raise HTTPException(
                422, "ID поля: латинские буквы, цифры, _ или -, начиная с буквы"
            )
        if field_id in ids:
            raise HTTPException(422, f"ID поля '{field_id}' повторяется")
        if field_id in input_ids:
            raise HTTPException(422, f"ID поля '{field_id}' совпадает с ID поля ввода")
        ids.add(field_id)
        item["id"] = field_id
        if item["value_template"]:
            if item["type"] not in {"text", "qr", "barcode"}:
                raise HTTPException(
                    422,
                    f"Шаблонизатор доступен только для text, qr или barcode-поля '{field_id}'",
                )
            variables = INPUT_TOKEN_RE.findall(item["value_template"])
            unknown_variables = set(variables) - input_ids
            if unknown_variables:
                raise HTTPException(
                    422,
                    f"Неизвестные поля ввода: {', '.join(sorted(unknown_variables))}",
                )
        if (
            item["x_dots"] + item["w_dots"] > label_width_dots
            or item["y_dots"] + item["h_dots"] > label_height_dots
        ):
            raise HTTPException(422, f"Поле '{field_id}' выходит за границы этикетки")
        if item["restrict_group_id"]:
            group_or_404(db, item["restrict_group_id"])
        if item["asset_id"]:
            asset = asset_or_404(db, item["asset_id"])
            if (
                item["restrict_group_id"]
                and asset.group_id != item["restrict_group_id"]
            ):
                raise HTTPException(
                    422, f"Иконка поля '{field_id}' не принадлежит выбранной группе"
                )
        if item["type"] == "image":
            if item["binding"] not in {"static", "input", "asset_ref"}:
                raise HTTPException(
                    422,
                    f"Поле image '{field_id}' не поддерживает binding '{item['binding']}'",
                )
            if item["binding"] == "asset_ref" and not item["asset_id"]:
                raise HTTPException(
                    422, f"Для asset_ref поля '{field_id}' выберите иконку"
                )
        elif item["binding"] == "asset_ref":
            raise HTTPException(
                422, f"asset_ref допустим только для image-поля '{field_id}'"
            )
        if item["type"] in {"bar", "box", "circle"}:
            if item["binding"] != "static":
                raise HTTPException(
                    422,
                    f"Фигура '{field_id}' не поддерживает источник значения",
                )
            if item["rotation"]:
                raise HTTPException(
                    422,
                    f"Фигура '{field_id}' не поддерживает поворот в TSPL",
                )
        if item["type"] == "circle" and item["w_dots"] != item["h_dots"]:
            raise HTTPException(
                422, f"Круг '{field_id}' должен иметь равные ширину и высоту"
            )
        if item["input_kind"] == "enum":
            if item["type"] != "text" or item["binding"] != "input":
                raise HTTPException(
                    422, f"Enum доступен только для input text-поля '{field_id}'"
                )
            enum_values = [
                value.strip() for value in item["enum_values"] if value.strip()
            ]
            if not enum_values:
                raise HTTPException(422, f"Для enum-поля '{field_id}' укажите варианты")
            if len(set(enum_values)) != len(enum_values):
                raise HTTPException(422, f"Варианты enum-поля '{field_id}' повторяются")
            if any(len(value) > 100 for value in enum_values):
                raise HTTPException(
                    422, f"Вариант enum-поля '{field_id}' длиннее 100 символов"
                )
            if item["default_value"] and item["default_value"] not in enum_values:
                raise HTTPException(
                    422,
                    f"Значение по умолчанию enum-поля '{field_id}' отсутствует в вариантах",
                )
            item["enum_values"] = enum_values
        else:
            item["enum_values"] = []
        if item["binding"] == "derived":
            if (
                item["type"] != "text"
                or not item["source_field_id"]
                or not item["metadata_key"]
            ):
                raise HTTPException(
                    422,
                    f"Derived-поле '{field_id}' требует text, source_field_id и metadata_key",
                )
    for item in fields:
        if item["type"] in {"text", "qr", "barcode"} and item["value_template"]:
            for source_id, metadata_key in IMAGE_TOKEN_RE.findall(
                item["value_template"]
            ):
                source = next(
                    (candidate for candidate in fields if candidate["id"] == source_id),
                    None,
                )
                if not source or source["type"] != "image":
                    raise HTTPException(
                        422,
                        f"Источник токена image '{source_id}' должен быть image-полем",
                    )
                if metadata_key == "title" or not source["restrict_group_id"]:
                    continue
                group = group_or_404(db, source["restrict_group_id"])
                if metadata_key not in {
                    attribute["key"] for attribute in group.metadata_schema
                }:
                    raise HTTPException(
                        422,
                        f"Атрибут '{metadata_key}' отсутствует в группе источника",
                    )
        if item["binding"] == "derived":
            source = next(
                (
                    candidate
                    for candidate in fields
                    if candidate["id"] == item["source_field_id"]
                ),
                None,
            )
            if not source or source["type"] != "image":
                raise HTTPException(
                    422, f"Источник derived-поля '{item['id']}' должен быть image-полем"
                )
            if item["metadata_key"] == "__asset_title__":
                continue
            if source["restrict_group_id"]:
                group = group_or_404(db, source["restrict_group_id"])
                if item["metadata_key"] not in {
                    attribute["key"] for attribute in group.metadata_schema
                }:
                    raise HTTPException(
                        422,
                        f"Атрибут '{item['metadata_key']}' отсутствует в группе источника",
                    )
    # Do not keep two coordinate systems in the stored JSON.  This avoids
    # DPI-dependent rounding drift when a template is opened and saved again.
    for item in fields:
        for axis in ("x", "y", "w", "h"):
            item.pop(f"{axis}_mm", None)
    return fields


def preview_asset(
    db: Session, field: dict[str, Any], values: dict[str, str]
) -> Asset | None:
    """Resolve an image field exactly once, including its group restriction."""
    asset_id = (
        values.get(field["id"], "").strip()
        if field["binding"] == "input"
        else field.get("asset_id")
    )
    if not asset_id:
        return None
    asset = asset_or_404(db, asset_id)
    if field.get("restrict_group_id") and asset.group_id != field["restrict_group_id"]:
        raise HTTPException(
            422, f"Иконка поля '{field['id']}' не принадлежит выбранной группе"
        )
    return asset


def preview_values(
    db: Session, template: Template, values: dict[str, str]
) -> tuple[dict[str, str], dict[str, Asset | None]]:
    fields = {field["id"]: field for field in template.fields}
    input_fields = {field["id"]: field for field in (template.input_fields or [])}
    unknown = set(values) - set(fields) - set(input_fields)
    if unknown:
        raise HTTPException(422, f"Неизвестные поля: {', '.join(sorted(unknown))}")
    assets = {
        field_id: preview_asset(db, field, values)
        for field_id, field in fields.items()
        if field["type"] == "image"
    }
    resolved: dict[str, str] = {}
    input_values: dict[str, str] = {}
    for field_id, field in input_fields.items():
        value = values.get(field_id, field.get("default_value", ""))
        if field["type"] == "enum" and value and value not in field["enum_values"]:
            raise HTTPException(422, f"Недопустимый вариант enum-поля '{field_id}'")
        input_values[field_id] = value
    for field_id, field in fields.items():
        if field["type"] == "image":
            continue
        if field["binding"] == "derived":
            source = assets.get(field.get("source_field_id", ""))
            if not source:
                resolved[field_id] = field.get("default_value", "")
            elif field.get("metadata_key") == "__asset_title__":
                resolved[field_id] = source.title
            else:
                resolved[field_id] = str(
                    source.metadata_values.get(
                        field.get("metadata_key"), field.get("default_value", "")
                    )
                )
        elif field["binding"] == "input":
            value = values.get(field_id, field.get("default_value", ""))
            if (
                field.get("input_kind") == "enum"
                and value
                and value not in field.get("enum_values", [])
            ):
                raise HTTPException(422, f"Недопустимый вариант enum-поля '{field_id}'")
            resolved[field_id] = value
        else:
            template_value = field.get("value_template", "")
            if not template_value:
                resolved[field_id] = field.get("default_value", "")
                continue

            def image_value(match: re.Match[str]) -> str:
                source = assets.get(match.group(1))
                if not source:
                    return ""
                if match.group(2) == "title":
                    return source.title
                return str(source.metadata_values.get(match.group(2), ""))

            template_value = IMAGE_TOKEN_RE.sub(image_value, template_value)
            resolved[field_id] = INPUT_TOKEN_RE.sub(
                lambda match: input_values[match.group(1)], template_value
            )
    return resolved, assets


def mm_to_px(value: float, dpi: int) -> int:
    return max(1, round(value / 25.4 * dpi))


def preview_font(size: int) -> ImageFont.ImageFont:
    """Use a Unicode font for raster text; deployments can pin one explicitly."""
    candidates = (
        os.getenv("LOCALLABEL_UNICODE_FONT", ""),
        "/System/Library/Fonts/Supplemental/Verdana.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans.ttf",
        "Arial.ttf",
    )
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


TSPL_FONT_CELLS = {
    "1": (8, 12),
    "2": (12, 20),
    "3": (16, 24),
    "4": (24, 32),
    "5": (32, 48),
    "6": (14, 19),
    "7": (21, 27),
    "8": (14, 25),
}


def draw_tspl_text(image: Image.Image, field: dict[str, Any], value: str) -> None:
    """Rasterize a fixed-pitch approximation of TSPL's built-in font grid.

    TSPL fonts live in the printer firmware, so Pillow cannot draw their exact
    glyphs.  Keeping every glyph on the same baseline is nevertheless
    important: centring each cropped glyph makes descenders and capitals appear
    to jump vertically in the preview.
    """
    base_width, base_height = TSPL_FONT_CELLS[field.get("tspl_font", "3")]
    cell_width = base_width * int(field.get("tspl_x_mul", 1))
    cell_height = base_height * int(field.get("tspl_y_mul", 1))
    lines = value.splitlines() or [""]
    width, height = image.size
    total_height = len(lines) * cell_height
    vertical = field.get("vertical_align", "middle")
    y = (
        0
        if vertical == "top"
        else height - total_height
        if vertical == "bottom"
        else (height - total_height) // 2
    )
    # Work at 2× before reducing to the printer-dot grid.  The common baseline
    # is retained while each glyph is narrowed into its fixed-width cell.
    source_scale = 2
    source_width, source_height = base_width * source_scale, base_height * source_scale
    source_font = preview_font(max(8, source_height - 2))
    _, descent = source_font.getmetrics()
    baseline = source_height - descent - 1
    for line in lines:
        line_width = len(line) * cell_width
        align = field.get("text_align", "center")
        x = (
            0
            if align == "left"
            else width - line_width
            if align == "right"
            else (width - line_width) // 2
        )
        for character in line:
            # Crop only horizontally.  Keeping the full vertical canvas means
            # every character uses the identical baseline, including `p`, `g`,
            # and Cyrillic descenders.
            glyph = Image.new("L", (source_width * 2, source_height), 255)
            glyph_draw = ImageDraw.Draw(glyph)
            glyph_draw.text(
                (0, baseline), character, fill=0, font=source_font, anchor="ls"
            )
            bbox = ImageOps.invert(glyph).getbbox()
            cell = Image.new("L", (base_width, base_height), 255)
            if bbox:
                mark = glyph.crop((bbox[0], 0, bbox[2], source_height))
                mark.thumbnail(
                    (max(1, source_width - 2), source_height), Image.Resampling.LANCZOS
                )
                source_cell = Image.new("L", (source_width, source_height), 255)
                source_cell.paste(mark, ((source_width - mark.width) // 2, 0))
                cell = source_cell.resize(
                    (base_width, base_height), Image.Resampling.LANCZOS
                )
            cell = cell.resize((cell_width, cell_height), Image.Resampling.NEAREST)
            mask = cell.point(lambda pixel: 255 if pixel < 128 else 0)
            image.paste(Image.new("RGBA", cell.size, "black"), (x, y), mask)
            x += cell_width
        y += cell_height


def draw_vector_tspl_text(
    image: Image.Image, field: dict[str, Any], value: str, dpi: int
) -> None:
    """Preview TSPL2 font 0 / downloaded TrueType fonts as proportional text."""
    # In TEXT with font 0, the width and height arguments are point sizes.
    x_points, y_points = (
        int(field.get("tspl_x_mul", 12)),
        int(field.get("tspl_y_mul", 12)),
    )
    font = preview_font(max(1, round(y_points / 72 * dpi)))
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(value.splitlines() or [""]):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        align = field.get("text_align", "left")
        x = (
            0
            if align == "left"
            else image.width - text_width
            if align == "right"
            else (image.width - text_width) // 2
        )
        # Pillow has no independent horizontal point size; scale the line to
        # TSPL's requested width while preserving its proportional metrics.
        line_image = Image.new(
            "L", (max(1, text_width), max(1, bbox[3] - bbox[1])), 255
        )
        ImageDraw.Draw(line_image).text((-bbox[0], -bbox[1]), line, fill=0, font=font)
        requested_width = max(1, round(x_points / y_points * line_image.width))
        line_image = line_image.resize(
            (requested_width, line_image.height), Image.Resampling.LANCZOS
        )
        mask = line_image.point(lambda pixel: 255 if pixel < 128 else 0)
        image.paste(
            Image.new("RGBA", line_image.size, "black"),
            (x, index * round(y_points / 72 * dpi)),
            mask,
        )


def paste_fitted(
    canvas: Image.Image, source: Image.Image, box: tuple[int, int, int, int], mode: str
) -> None:
    x, y, width, height = box
    source = source.convert("1") if source.mode == "1" else source.convert("RGBA")
    resampling = (
        Image.Resampling.NEAREST if source.mode == "1" else Image.Resampling.LANCZOS
    )
    if mode == "stretch":
        rendered = source.resize((width, height), resampling)
    else:
        scale = (max if mode == "cover" else min)(
            width / source.width, height / source.height
        )
        rendered = source.resize(
            (max(1, round(source.width * scale)), max(1, round(source.height * scale))),
            resampling,
        )
        if mode == "cover":
            left, top = (rendered.width - width) // 2, (rendered.height - height) // 2
            rendered = rendered.crop((left, top, left + width, top + height))
    rendered = rendered.convert("RGBA")
    canvas.alpha_composite(
        rendered,
        (x + (width - rendered.width) // 2, y + (height - rendered.height) // 2),
    )


def draw_symbol(
    image: Image.Image, box: tuple[int, int, int, int], value: str, kind: str
) -> None:
    """Visual placeholder for QR/barcode until their printer serializers land.

    It is deliberately deterministic so a changed input is obvious in the preview,
    but it is not represented as a scannable production code.
    """
    draw = ImageDraw.Draw(image)
    x, y, width, height = box
    digest = hashlib.sha256(value.encode()).digest()
    if kind == "qr":
        cells = 21
        unit = max(1, min(width, height) // (cells + 2))
        origin_x, origin_y = (
            x + (width - cells * unit) // 2,
            y + (height - cells * unit) // 2,
        )
        for row in range(cells):
            for col in range(cells):
                if digest[(row * cells + col) % len(digest)] >> ((row + col) % 8) & 1:
                    draw.rectangle(
                        (
                            origin_x + col * unit,
                            origin_y + row * unit,
                            origin_x + (col + 1) * unit - 1,
                            origin_y + (row + 1) * unit - 1,
                        ),
                        fill="black",
                    )
    else:
        cursor = x
        bits = "".join(f"{byte:08b}" for byte in digest)
        for index, bit in enumerate(bits):
            bar = max(1, width // len(bits))
            right = min(x + width, cursor + bar)
            if bit == "1":
                draw.rectangle(
                    (cursor, y, right, y + max(1, round(height * 0.78))), fill="black"
                )
            cursor = right
            if cursor >= x + width:
                break
        font = preview_font(12)
        label = value[:48]
        bbox = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (x + (width - (bbox[2] - bbox[0])) // 2, y + max(1, round(height * 0.82))),
            label,
            fill="black",
            font=font,
        )


def render_template_preview(
    db: Session, template: Template, values: dict[str, str]
) -> Image.Image:
    dpi = template.printer_dpi
    canvas = Image.new(
        "RGBA",
        (
            mm_to_px(template.label_width_mm, dpi),
            mm_to_px(template.label_height_mm, dpi),
        ),
        "white",
    )
    resolved, assets = preview_values(db, template, values)
    for field in template.fields:
        x = int(field.get("x_dots", round(field.get("x_mm", 0) / 25.4 * dpi)))
        y = int(field.get("y_dots", round(field.get("y_mm", 0) / 25.4 * dpi)))
        width = int(field.get("w_dots", mm_to_px(field.get("w_mm", 1), dpi)))
        height = int(field.get("h_dots", mm_to_px(field.get("h_mm", 1), dpi)))
        overlay = Image.new("RGBA", (width, height), (255, 255, 255, 0))
        if field["type"] == "image":
            asset = assets[field["id"]]
            if asset:
                # Prepared BMP is precisely the 1-bit source that will be sent to TSPL.
                source = Image.open(io.BytesIO(asset.print_data or asset.image_data))
                paste_fitted(
                    overlay,
                    source,
                    (0, 0, width, height),
                    field.get("fit_mode", "contain"),
                )
        elif field["type"] in {"qr", "barcode"}:
            draw_symbol(
                overlay, (0, 0, width, height), resolved[field["id"]], field["type"]
            )
        elif field["type"] == "bar":
            ImageDraw.Draw(overlay).rectangle(
                (0, 0, width - 1, height - 1), fill="black"
            )
        elif field["type"] == "box":
            draw = ImageDraw.Draw(overlay)
            thickness = min(
                int(field.get("line_thickness", 1)), max(1, min(width, height) // 2)
            )
            radius = min(int(field.get("corner_radius", 0)), min(width, height) // 2)
            draw.rounded_rectangle(
                (0, 0, width - 1, height - 1),
                radius=radius,
                outline="black",
                width=thickness,
            )
        elif field["type"] == "circle":
            thickness = min(int(field.get("line_thickness", 1)), max(1, width // 2))
            ImageDraw.Draw(overlay).ellipse(
                (0, 0, width - 1, height - 1), outline="black", width=thickness
            )
        else:
            if field.get("tspl_font") in {"0"} or str(
                field.get("tspl_font", "")
            ).lower().endswith((".ttf", ".otf")):
                draw_vector_tspl_text(overlay, field, resolved[field["id"]], dpi)
            else:
                draw_tspl_text(overlay, field, resolved[field["id"]])
        rotation = float(field.get("rotation", 0))
        if rotation:
            overlay = overlay.rotate(
                -rotation, expand=True, resample=Image.Resampling.BICUBIC
            )
            x -= (overlay.width - width) // 2
            y -= (overlay.height - height) // 2
        canvas.alpha_composite(overlay, (x, y))
    # Thermal heads print dots, not greyscale: make the preview the same 1-bit bitmap.
    return canvas.convert("L").point(lambda pixel: 0 if pixel < 128 else 255, mode="1")


def tspl_escape(value: str) -> str:
    """Encode TEXT/QR/BARCODE payloads without letting them change commands."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ")


def tspl_rotation(value: float) -> int:
    """TEXT/QR/BARCODE only support the four firmware rotations."""
    rotation = int(round(value / 90) * 90) % 360
    return rotation


def field_dots(field: dict[str, Any], dpi: int) -> tuple[int, int, int, int]:
    return (
        int(field.get("x_dots", round(field.get("x_mm", 0) / 25.4 * dpi))),
        int(field.get("y_dots", round(field.get("y_mm", 0) / 25.4 * dpi))),
        int(field.get("w_dots", mm_to_px(field.get("w_mm", 1), dpi))),
        int(field.get("h_dots", mm_to_px(field.get("h_mm", 1), dpi))),
    )


def tspl_image_bitmap(
    asset: Asset, field: dict[str, Any], dpi: int
) -> tuple[int, int, int, int, bytes]:
    """Render one image field into TSPL's packed 1-bit BITMAP payload.

    TSPL's BITMAP payload uses the same white/black bit convention as Pillow's
    mode ``1`` on the target printers: a set bit leaves the dot white.
    """
    x, y, width, height = field_dots(field, dpi)
    overlay = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    with Image.open(io.BytesIO(asset.print_data or asset.image_data)) as source:
        paste_fitted(
            overlay,
            source,
            (0, 0, width, height),
            field.get("fit_mode", "contain"),
        )
    rotation = float(field.get("rotation", 0))
    if rotation:
        overlay = overlay.rotate(
            -rotation, expand=True, resample=Image.Resampling.BICUBIC
        )
        x -= (overlay.width - width) // 2
        y -= (overlay.height - height) // 2

    # Composite first so transparent source pixels print as white, just like the
    # preview.  Thresholding also makes PNG/JPEG assets deterministic on a
    # thermal printer.
    bitmap = Image.new("RGBA", overlay.size, "white")
    bitmap.alpha_composite(overlay)
    bitmap = bitmap.convert("L").point(
        lambda pixel: 0 if pixel < 128 else 255, mode="1"
    )
    packed = bitmap.tobytes()
    bytes_per_row = (bitmap.width + 7) // 8
    return x, y, bytes_per_row, bitmap.height, packed


def native_tspl(
    template: Template,
    values: dict[str, str],
    db: Session,
    copies: int,
    printer_settings: dict[str, Any] | None = None,
) -> bytes:
    """Build a printer-native TSPL job, including inline image BITMAP payloads."""
    resolved, assets = preview_values(db, template, values)
    command = bytearray()

    def append_line(line: str) -> None:
        command.extend(line.encode("utf-8"))
        command.extend(b"\r\n")

    settings = printer_settings or {}
    gap = float(settings.get("gap_mm", 2))
    gap_offset = float(settings.get("gap_offset_mm", 0))
    direction = int(settings.get("direction", 1))
    codepage = str(settings.get("codepage", "UTF-8"))
    setup = [
        f"SIZE {template.label_width_mm:g} mm,{template.label_height_mm:g} mm",
        f"GAP {gap:g} mm,{gap_offset:g} mm",
    ]
    if settings.get("speed_ips") is not None:
        setup.append(f"SPEED {float(settings['speed_ips']):g}")
    if settings.get("density") is not None:
        setup.append(f"DENSITY {int(settings['density'])}")
    setup += [
        f"DIRECTION {direction}, 0",
        f"CODEPAGE {codepage}",
        "CLS",
    ]
    for line in setup:
        append_line(line)
    for field in template.fields:
        x, y, width, height = field_dots(field, template.printer_dpi)
        rotation = tspl_rotation(float(field.get("rotation", 0)))
        if field["type"] == "image":
            asset = assets.get(field["id"])
            if asset:
                image_x, image_y, bytes_per_row, image_height, packed = (
                    tspl_image_bitmap(asset, field, template.printer_dpi)
                )
                command.extend(
                    f"BITMAP {image_x},{image_y},{bytes_per_row},{image_height},0,".encode(
                        "ascii"
                    )
                )
                command.extend(packed)
                command.extend(b"\r\n")
            continue
        value = resolved[field["id"]]
        if field["type"] == "text":
            font = str(field.get("tspl_font", "3"))
            x_mul, y_mul = (
                int(field.get("tspl_x_mul", 1)),
                int(field.get("tspl_y_mul", 1)),
            )
            if font in TSPL_FONT_CELLS:
                cell_width, cell_height = TSPL_FONT_CELLS[font]
                line_height = cell_height * y_mul
                text_lines = value.splitlines() or [""]
                total_height = len(text_lines) * line_height
                if field.get("vertical_align") == "middle":
                    y += max(0, (height - total_height) // 2)
                elif field.get("vertical_align") == "bottom":
                    y += max(0, height - total_height)
                for index, text_line in enumerate(text_lines):
                    line_width = len(text_line) * cell_width * x_mul
                    text_x = x
                    if field.get("text_align") == "center":
                        text_x += max(0, (width - line_width) // 2)
                    elif field.get("text_align") == "right":
                        text_x += max(0, width - line_width)
                    append_line(
                        f'TEXT {text_x},{y + index * line_height},"{font}",{rotation},{x_mul},{y_mul},"{tspl_escape(text_line)}"'
                    )
            else:
                # TSPL2 font 0 and downloaded TTF names use point dimensions.
                append_line(
                    f'TEXT {x},{y},"{font}",{rotation},{x_mul},{y_mul},"{tspl_escape(value)}"'
                )
        elif field["type"] == "qr":
            cell = max(1, min(width, height) // 29)
            append_line(f'QRCODE {x},{y},L,{cell},A,{rotation},"{tspl_escape(value)}"')
        elif field["type"] == "barcode":
            narrow = max(1, min(10, width // max(1, len(value) * 11)))
            append_line(
                f'BARCODE {x},{y},"128",{max(1, height)},1,{rotation},{narrow},{narrow * 2},"{tspl_escape(value)}"'
            )
        elif field["type"] == "bar":
            append_line(f"BAR {x},{y},{width},{height}")
        elif field["type"] == "box":
            thickness = int(field.get("line_thickness", 1))
            radius = int(field.get("corner_radius", 0))
            line = f"BOX {x},{y},{x + width},{y + height},{thickness}"
            if radius:
                line += f",{radius}"
            append_line(line)
        elif field["type"] == "circle":
            append_line(
                f"CIRCLE {x + width // 2},{y + height // 2},{width},{int(field.get('line_thickness', 1))}"
            )
    append_line(f"PRINT {copies},1")
    return bytes(command)


def adjusted_image(data: bytes, adjustment: ImageAdjustment) -> Image.Image:
    image = Image.open(io.BytesIO(data)).convert("L")
    if adjustment.rotation:
        image = image.rotate(-adjustment.rotation, expand=True)
    pixels = image.load()
    corners = [
        pixels[x, y] for x in range(min(8, image.width)) for y in (0, image.height - 1)
    ]
    corners += [
        pixels[x, y] for y in range(min(8, image.height)) for x in (0, image.width - 1)
    ]
    background = sorted(corners)[len(corners) // 2] if corners else 255
    if 120 < background < 254:
        image = image.point(lambda value: min(255, value * 255 // background))
    bbox = (
        ImageOps.invert(image).point(lambda value: 255 if value > 15 else 0).getbbox()
    )
    if bbox:
        image = image.crop(bbox)
    side = max(image.size)
    padding = max(round(side * 0.06), 1)
    canvas = Image.new("L", (side + padding * 2, side + padding * 2), 255)
    canvas.paste(
        image, ((canvas.width - image.width) // 2, (canvas.height - image.height) // 2)
    )
    image = canvas.resize((adjustment.size, adjustment.size), Image.Resampling.LANCZOS)
    image = ImageEnhance.Brightness(image).enhance(adjustment.brightness)
    image = ImageEnhance.Contrast(image).enhance(adjustment.contrast)
    image = ImageEnhance.Sharpness(image).enhance(adjustment.sharpness)
    if adjustment.invert:
        image = ImageOps.invert(image)
    return (
        image.point(lambda pixel: 255 if pixel > adjustment.threshold else 0, mode="1")
        if adjustment.threshold
        else image.convert("1")
    )


def build_asset(
    db: Session,
    upload: UploadFile,
    group_id: str | None,
    metadata: dict[str, Any],
    title: str | None = None,
) -> Asset:
    filename = upload.filename or "icon"
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, f"{filename}: поддерживаются PNG, JPG и WEBP")
    data = upload.file.read()
    image = read_image(data)
    adjustment = default_print_adjustment()
    prepared = adjusted_image(data, adjustment)
    buffer = io.BytesIO()
    prepared.save(buffer, format="BMP")
    return Asset(
        id=uuid.uuid4().hex,
        title=safe_title(title or Path(filename).stem, "Иконка"),
        group_id=group_id,
        metadata_values=validate_metadata(db, group_id, metadata),
        file_name=Path(filename).name,
        mime_type=media_type(filename),
        image_data=data,
        print_data=buffer.getvalue(),
        print_adjustment=adjustment.model_dump(),
        width=image.width,
        height=image.height,
        favorite=False,
        source="user_upload",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
