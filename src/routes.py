"""HTTP routes for the asset library and template editor."""

from __future__ import annotations

import io
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import MAX_BATCH_BYTES, MAX_BATCH_FILES, STATIC_DIR
from .database import get_db
from .models import Asset, AssetGroup, Printer, Template
from .schemas import (
    AssetGroupPayload,
    AssetUpdate,
    default_print_adjustment,
    ImageAdjustment,
    PrinterCreatePayload,
    PrinterUpdatePayload,
    ServerPrintPayload,
    TemplatePayload,
    TemplatePreviewPayload,
    TemplatePrintPayload,
)
from .services import (
    adjusted_image,
    asset_or_404,
    build_asset,
    native_tspl,
    normalized_margins,
    normalized_schema,
    parse_json_object,
    public_asset,
    public_group,
    public_template,
    render_template_preview,
    safe_text,
    safe_title,
    template_or_404,
    utcnow,
    validate_metadata,
    validate_template_fields,
    validate_template_input_fields,
    group_or_404,
)
from .printers import (
    discover_server_devices,
    send_to_printer,
    server_discovery_diagnostics,
    status_for_device,
)

router = APIRouter()

@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def public_printer(printer: Printer) -> dict[str, Any]:
    return {
        "id": printer.id,
        "name": printer.name,
        "transport": printer.transport,
        "device_uri": printer.device_uri,
        "settings": printer.settings or {},
        "is_active": printer.is_active,
        "status": status_for_device(printer.device_uri),
        "created_at": printer.created_at,
        "updated_at": printer.updated_at,
    }


@router.get("/api/printers/devices")
def list_server_devices() -> dict[str, Any]:
    """Discover printer destinations visible to the application server."""
    return {
        "devices": discover_server_devices(),
        "diagnostics": server_discovery_diagnostics(),
    }


@router.get("/api/printers")
def list_printers(db: Session = Depends(get_db)) -> dict[str, Any]:
    printers = db.scalars(select(Printer).order_by(Printer.name)).all()
    return {"printers": [public_printer(printer) for printer in printers]}


@router.post("/api/printers", status_code=201)
def create_printer(
    payload: PrinterCreatePayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    devices = {item["uri"]: item for item in discover_server_devices()}
    device = devices.get(payload.device_uri)
    if not device:
        raise HTTPException(422, "Устройство не найдено на сервере; обновите список")
    if db.scalar(select(Printer).where(Printer.device_uri == payload.device_uri)):
        raise HTTPException(409, "Это устройство уже добавлено")
    printer = Printer(
        id=uuid.uuid4().hex,
        name=safe_text(payload.name, device["name"], 100),
        transport=device["transport"],
        device_uri=device["uri"],
        settings=payload.settings.model_dump(),
        is_active=not bool(db.scalar(select(Printer.id).limit(1))),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(printer)
    db.commit()
    db.refresh(printer)
    return public_printer(printer)


@router.put("/api/printers/{printer_id}")
def update_printer(
    printer_id: str, payload: PrinterUpdatePayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    printer = db.get(Printer, printer_id)
    if not printer:
        raise HTTPException(404, "Принтер не найден")
    if payload.is_active:
        for item in db.scalars(select(Printer).where(Printer.is_active.is_(True))):
            item.is_active = False
    printer.name = safe_text(payload.name, printer.name, 100)
    printer.settings = payload.settings.model_dump()
    printer.is_active = payload.is_active
    printer.updated_at = utcnow()
    db.commit()
    db.refresh(printer)
    return public_printer(printer)


@router.get("/api/printers/{printer_id}/status")
def printer_status(printer_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    printer = db.get(Printer, printer_id)
    if not printer:
        raise HTTPException(404, "Принтер не найден")
    return status_for_device(printer.device_uri)


@router.get("/api/asset-groups")
def list_asset_groups(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {
        "groups": [
            public_group(db, group)
            for group in db.scalars(select(AssetGroup).order_by(AssetGroup.name)).all()
        ]
    }


@router.post("/api/asset-groups", status_code=201)
def create_asset_group(
    payload: AssetGroupPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    name = safe_text(payload.name, "Группа", 100)
    if db.scalar(
        select(AssetGroup).where(func.lower(AssetGroup.name) == name.casefold())
    ):
        raise HTTPException(409, "Группа с таким названием уже есть")
    group = AssetGroup(
        id=uuid.uuid4().hex,
        name=name,
        metadata_schema=normalized_schema(payload.metadata_schema),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return public_group(db, group)


@router.patch("/api/asset-groups/{group_id}")
def update_asset_group(
    group_id: str, payload: AssetGroupPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    group, name = group_or_404(db, group_id), safe_text(payload.name, "Группа", 100)
    duplicate = db.scalar(
        select(AssetGroup).where(
            func.lower(AssetGroup.name) == name.casefold(), AssetGroup.id != group_id
        )
    )
    if duplicate:
        raise HTTPException(409, "Группа с таким названием уже есть")
    schema = normalized_schema(payload.metadata_schema)
    candidate = AssetGroup(
        id=group.id,
        name=name,
        metadata_schema=schema,
        created_at=group.created_at,
        updated_at=group.updated_at,
    )
    errors: list[str] = []
    for asset in db.scalars(select(Asset).where(Asset.group_id == group_id)):
        try:
            validate_metadata(db, group_id, asset.metadata_values, candidate)
        except HTTPException as error:
            errors.append(f"{asset.title}: {error.detail}")
    if errors:
        raise HTTPException(
            409, "Схема сделает metadata иконок невалидными: " + "; ".join(errors)
        )
    group.name, group.metadata_schema, group.updated_at = name, schema, utcnow()
    db.commit()
    db.refresh(group)
    return public_group(db, group)


@router.delete("/api/asset-groups/{group_id}", status_code=204)
def delete_asset_group(group_id: str, db: Session = Depends(get_db)) -> None:
    group = group_or_404(db, group_id)
    linked = db.scalars(
        select(Asset.title).where(Asset.group_id == group_id).limit(5)
    ).all()
    if linked:
        raise HTTPException(
            409, "Нельзя удалить группу с иконками: " + ", ".join(linked)
        )
    db.delete(group)
    db.commit()


@router.get("/api/icons")
def list_icons(
    query: str = "",
    group_id: str = "",
    favorite: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    statement = select(Asset).order_by(Asset.favorite.desc(), Asset.title)
    if query.strip():
        statement = statement.where(Asset.title.ilike(f"%{query.strip()}%"))
    if group_id:
        statement = statement.where(Asset.group_id == group_id)
    if favorite:
        statement = statement.where(Asset.favorite.is_(True))
    assets = db.scalars(statement).all()
    return {
        "icons": [public_asset(asset) for asset in assets],
        "groups": [
            public_group(db, group)
            for group in db.scalars(select(AssetGroup).order_by(AssetGroup.name))
        ],
        "total": len(assets),
    }


@router.post("/api/icons", status_code=201)
def create_icon(
    file: UploadFile = File(...),
    title: str = Form(default=""),
    group_id: str | None = Form(default=None),
    metadata: str = Form(default="{}"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    asset = build_asset(
        db, file, group_id or None, parse_json_object(metadata, "Metadata"), title
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return public_asset(asset)


@router.post("/api/icons/batch", status_code=201)
def create_icons_batch(
    files: list[UploadFile] = File(...),
    group_id: str | None = Form(default=None),
    metadata: str = Form(default="{}"),
    metadata_by_filename: str = Form(default="{}"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not files:
        raise HTTPException(422, "Выберите хотя бы один файл")
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(
            413, f"За один раз можно загрузить не более {MAX_BATCH_FILES} файлов"
        )
    shared_metadata = parse_json_object(metadata, "Metadata")
    by_filename = parse_json_object(metadata_by_filename, "Metadata по файлам")
    if any(not isinstance(value, dict) for value in by_filename.values()):
        raise HTTPException(
            422, "Каждое значение metadata по файлам должно быть JSON-объектом"
        )
    assets: list[Asset] = []
    total_bytes = 0
    # Validate and construct every asset before committing: no partial batches.
    for upload in files:
        upload.file.seek(0, 2)
        total_bytes += upload.file.tell()
        upload.file.seek(0)
        if total_bytes > MAX_BATCH_BYTES:
            raise HTTPException(413, "Суммарный размер пакета больше 250 МБ")
        filename = upload.filename or ""
        values = {**shared_metadata, **by_filename.get(filename, {})}
        assets.append(build_asset(db, upload, group_id or None, values))
    db.add_all(assets)
    db.commit()
    for asset in assets:
        db.refresh(asset)
    return {"assets": [public_asset(asset) for asset in assets], "total": len(assets)}


@router.get("/api/icons/{icon_id}/image")
def get_image(icon_id: str, db: Session = Depends(get_db)) -> Response:
    asset = asset_or_404(db, icon_id)
    return Response(
        asset.image_data,
        media_type=asset.mime_type,
        headers={"Content-Disposition": f'inline; filename="{asset.file_name}"'},
    )


@router.get("/api/icons/{icon_id}/print-image")
def get_print_image(icon_id: str, db: Session = Depends(get_db)) -> Response:
    """Return the prepared thermal bitmap, falling back to the original asset."""
    asset = asset_or_404(db, icon_id)
    return Response(
        asset.print_data or asset.image_data,
        media_type="image/bmp" if asset.print_data else asset.mime_type,
        headers={"Content-Disposition": f'inline; filename="{asset.file_name}"'},
    )


@router.get("/api/icons/{icon_id}/download")
def download_icon(icon_id: str, db: Session = Depends(get_db)) -> Response:
    asset = asset_or_404(db, icon_id)
    return Response(
        asset.image_data,
        media_type=asset.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{asset.file_name}"'},
    )


@router.patch("/api/icons/{icon_id}")
def update_icon(
    icon_id: str, update: AssetUpdate, db: Session = Depends(get_db)
) -> dict[str, Any]:
    asset = asset_or_404(db, icon_id)
    asset.title = safe_title(update.title, asset.title)
    asset.group_id = update.group_id
    asset.metadata_values = validate_metadata(db, update.group_id, update.metadata)
    asset.updated_at = utcnow()
    db.commit()
    db.refresh(asset)
    return public_asset(asset)


@router.post("/api/icons/{icon_id}/favorite")
def toggle_favorite(icon_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    asset = asset_or_404(db, icon_id)
    asset.favorite, asset.updated_at = not asset.favorite, utcnow()
    db.commit()
    db.refresh(asset)
    return public_asset(asset)


@router.post("/api/icons/{icon_id}/preview")
def preview_adjustment(
    icon_id: str, adjustment: ImageAdjustment, db: Session = Depends(get_db)
) -> Response:
    image = adjusted_image(asset_or_404(db, icon_id).image_data, adjustment)
    buffer = io.BytesIO()
    image.convert("RGB").resize(
        (image.width * 2, image.height * 2), Image.Resampling.NEAREST
    ).save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png")


@router.post("/api/icons/{icon_id}/apply-adjustment")
def apply_adjustment(
    icon_id: str, adjustment: ImageAdjustment, db: Session = Depends(get_db)
) -> dict[str, Any]:
    asset = asset_or_404(db, icon_id)
    image = adjusted_image(asset.image_data, adjustment)
    buffer = io.BytesIO()
    image.save(buffer, format="BMP")
    asset.print_data = buffer.getvalue()
    asset.print_adjustment = adjustment.model_dump()
    asset.updated_at = utcnow()
    db.commit()
    db.refresh(asset)
    return {**public_asset(asset), "print_size": image.size}


@router.delete("/api/icons/{icon_id}", status_code=204)
def delete_icon(icon_id: str, db: Session = Depends(get_db)) -> None:
    db.delete(asset_or_404(db, icon_id))
    db.commit()


@router.get("/api/templates")
def list_templates(db: Session = Depends(get_db)) -> dict[str, Any]:
    templates = db.scalars(select(Template).order_by(Template.updated_at.desc())).all()
    return {"templates": [public_template(template) for template in templates]}


@router.post("/api/templates", status_code=201)
def create_template(
    payload: TemplatePayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    name = safe_text(payload.name, "Шаблон", 100)
    if db.scalar(select(Template).where(func.lower(Template.name) == name.casefold())):
        raise HTTPException(409, "Шаблон с таким названием уже есть")
    input_fields = validate_template_input_fields(payload.input_fields)
    template = Template(
        id=uuid.uuid4().hex,
        name=name,
        label_width_mm=payload.label_width_mm,
        label_height_mm=payload.label_height_mm,
        printer_dpi=payload.printer_dpi,
        margins=normalized_margins(payload),
        fields=validate_template_fields(db, payload, input_fields),
        input_fields=input_fields,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return public_template(template)


@router.get("/api/templates/{template_id}")
def get_template(template_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return public_template(template_or_404(db, template_id))


@router.post("/api/templates/{template_id}/preview")
def template_preview(
    template_id: str, payload: TemplatePreviewPayload, db: Session = Depends(get_db)
) -> Response:
    """Render a final label candidate. It deliberately creates no print job."""
    template = template_or_404(db, template_id)
    image = render_template_preview(db, template, payload.values)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", dpi=(template.printer_dpi, template.printer_dpi))
    return Response(buffer.getvalue(), media_type="image/png")


@router.post("/api/templates/{template_id}/tspl")
def template_tspl(
    template_id: str, payload: TemplatePrintPayload, db: Session = Depends(get_db)
) -> Response:
    """Return the native TSPL program that can be written straight to WebUSB."""
    template = template_or_404(db, template_id)
    command = native_tspl(
        template, payload.values, db, payload.copies, payload.printer_settings
    )
    return Response(
        command,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{template.name}.tspl"'},
    )


@router.post("/api/templates/{template_id}/print")
def print_on_server(
    template_id: str, payload: ServerPrintPayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Render and send a raw TSPL job from the server to a saved destination."""
    template = template_or_404(db, template_id)
    printer = db.get(Printer, payload.printer_id)
    if not printer:
        raise HTTPException(404, "Принтер не найден")
    device_status = status_for_device(printer.device_uri)
    if device_status["state"] not in {"ready", "printing", "unknown"}:
        raise HTTPException(409, device_status["message"])
    command = native_tspl(
        template, payload.values, db, payload.copies, printer.settings or {}
    )
    sent = send_to_printer(printer.device_uri, command)
    return {
        "printer_id": printer.id,
        "bytes_sent": sent,
        "copies": payload.copies,
        "status": status_for_device(printer.device_uri),
    }


@router.put("/api/templates/{template_id}")
def update_template(
    template_id: str, payload: TemplatePayload, db: Session = Depends(get_db)
) -> dict[str, Any]:
    template = template_or_404(db, template_id)
    name = safe_text(payload.name, "Шаблон", 100)
    duplicate = db.scalar(
        select(Template).where(
            func.lower(Template.name) == name.casefold(), Template.id != template_id
        )
    )
    if duplicate:
        raise HTTPException(409, "Шаблон с таким названием уже есть")
    template.name, template.label_width_mm, template.label_height_mm = (
        name,
        payload.label_width_mm,
        payload.label_height_mm,
    )
    input_fields = validate_template_input_fields(payload.input_fields)
    template.printer_dpi, template.margins, template.fields, template.input_fields, template.updated_at = (
        payload.printer_dpi,
        normalized_margins(payload),
        validate_template_fields(db, payload, input_fields),
        input_fields,
        utcnow(),
    )
    db.commit()
    db.refresh(template)
    return public_template(template)


@router.delete("/api/templates/{template_id}", status_code=204)
def delete_template(template_id: str, db: Session = Depends(get_db)) -> None:
    db.delete(template_or_404(db, template_id))
    db.commit()


@router.get("/")
def index() -> Response:
    return Response((STATIC_DIR / "index.html").read_bytes(), media_type="text/html")


@router.get("/templates")
def templates_page() -> Response:
    return Response(
        (STATIC_DIR / "templates.html").read_bytes(), media_type="text/html"
    )


@router.get("/printers")
def printers_page() -> Response:
    return Response(
        (STATIC_DIR / "printers.html").read_bytes(), media_type="text/html"
    )


@router.get("/preview")
def preview_page() -> Response:
    # Kept as a compatibility URL for links created by older template editors.
    return Response((STATIC_DIR / "print.html").read_bytes(), media_type="text/html")


@router.get("/print")
def print_page() -> Response:
    return Response((STATIC_DIR / "print.html").read_bytes(), media_type="text/html")
