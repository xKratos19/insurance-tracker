from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorGridFSBucket
from io import BytesIO, StringIO
import csv

from .auth import install_auth, require_user
from .database import db, records_col
from .email_alert import send_email_alert
from .file_utils import (
    ALLOWED_DOC_MIMES,
    ALLOWED_POLICY_MIMES,
    CAR_DOCS_LABEL,
    PERSON_DOCS_LABEL,
    POLICY_LABEL,
    build_filename,
    pick_extension,
    validate_mime,
)
from .pdf_extract import extract_insurance_data
from .validators import validate_phone, validate_plate, validate_vin


app = FastAPI()
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

install_auth(app)


# ========== STARTUP ==========
@app.on_event("startup")
async def startup_event():
    app.state.fs_bucket = AsyncIOMotorGridFSBucket(db)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_expiring_insurances, "cron", hour=7, timezone="Europe/Bucharest"
    )
    scheduler.start()
    app.state.scheduler = scheduler


# ========== CRON JOB ==========
async def _collect_expiring_items(days_ahead: int = 7) -> list[dict]:
    """Shape data the same way `home` does so `send_email_alert` sees the same keys
    whether it's invoked by the scheduler or the `/admin/test-email` route."""
    today = datetime.utcnow().date()
    upcoming = datetime.utcnow() + timedelta(days=days_ahead)
    cursor = records_col.find({"insurances.insurance_end": {"$lte": upcoming}})
    rows = await cursor.to_list(length=None)

    out: list[dict] = []
    for d in rows:
        latest = (d.get("insurances") or [{}])[-1]
        end = latest.get("insurance_end")
        end_date = end.date() if hasattr(end, "date") else end
        if not end_date or end_date < today - timedelta(days=1):
            continue
        days_left = (end_date - today).days
        if days_left > days_ahead:
            continue
        out.append(
            {
                "name": d.get("name", ""),
                "car_name": d.get("car_name", ""),
                "plate_number": d.get("plate_number", ""),
                "insurance_end": end_date,
                "days_left": days_left,
            }
        )
    return out


async def check_expiring_insurances():
    items = await _collect_expiring_items()
    if items:
        send_email_alert(items)


# ========== HEALTH ==========
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ========== ROUTES ==========
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: dict = Depends(require_user)):
    cursor = records_col.find().sort("created_at", -1)
    data = await cursor.to_list(length=None)
    items = []

    for d in data:
        latest_ins = d.get("insurances", [{}])[-1] if d.get("insurances") else {}
        end = latest_ins.get("insurance_end")
        days_left = (
            (end.date() - datetime.utcnow().date()).days if end else None
        )

        items.append(
            {
                "id": str(d["_id"]),
                "name": d.get("name"),
                "phone": d.get("phone"),
                "car_name": d.get("car_name"),
                "plate_number": d.get("plate_number"),
                "vin_number": d.get("vin_number"),
                "insurance_start": (
                    latest_ins.get("insurance_start").date()
                    if latest_ins.get("insurance_start")
                    else ""
                ),
                "insurance_end": end.date() if end else "",
                "days_left": days_left,
                "documents": d.get("documents", []),
                "car_docs_ids": d.get("car_docs_ids", []),
                "person_docs_ids": d.get("person_docs_ids", []),
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "items": items,
            "today": datetime.utcnow().date(),
            "user": user,
        },
    )


# ---------- ADD NEW RECORD ----------
async def _store_upload(
    bucket: AsyncIOMotorGridFSBucket,
    upload: UploadFile,
    first_name: str,
    last_name: str,
    label: str,
    allowed_mimes: set[str],
) -> dict:
    if not upload or not upload.filename:
        return {}
    if not validate_mime(upload.content_type or "", allowed_mimes):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type for '{label}': {upload.content_type}",
        )
    content = await upload.read()
    ext = pick_extension(upload.filename, upload.content_type or "")
    renamed = build_filename(first_name, last_name, label, ext)
    file_id = await bucket.upload_from_stream(
        renamed,
        content,
        metadata={
            "type": upload.content_type,
            "original_filename": upload.filename,
            "label": label,
        },
    )
    return {
        "file_id": file_id,
        "filename": renamed,
        "original_filename": upload.filename,
        "content_type": upload.content_type,
        "label": label,
        "uploaded_at": datetime.utcnow(),
    }


def _split_full_name(name: str) -> tuple[str, str]:
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    # Romanian convention in this app: surname first, given name(s) after.
    return parts[0], " ".join(parts[1:])


@app.post("/add")
async def add_record(
    request: Request,
    name: str = Form(...),
    phone: str = Form(...),
    car_name: str = Form(...),
    plate_number: str = Form(...),
    vin_number: str = Form(...),
    insurance_start: str = Form(...),
    insurance_end: str = Form(...),
    first_name: str = Form(""),
    last_name: str = Form(""),
    policy_file: UploadFile | None = File(None),
    car_documents: list[UploadFile] | None = File(None),
    person_documents: list[UploadFile] | None = File(None),
    user: dict = Depends(require_user),
):
    validate_phone(phone)
    validate_plate(plate_number)
    validate_vin(vin_number)

    start_dt = datetime.strptime(insurance_start, "%Y-%m-%d")
    end_dt = datetime.strptime(insurance_end, "%Y-%m-%d")
    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="Insurance end must be after start.")

    # If first/last were not posted explicitly, derive from the combined `name` field
    # so renaming still produces meaningful filenames.
    if not (first_name and last_name):
        fallback_last, fallback_first = _split_full_name(name)
        first_name = first_name or fallback_first
        last_name = last_name or fallback_last

    bucket: AsyncIOMotorGridFSBucket = app.state.fs_bucket
    policy_docs: list[dict] = []
    car_docs_ids: list[dict] = []
    person_docs_ids: list[dict] = []

    if policy_file and policy_file.filename:
        rec = await _store_upload(
            bucket, policy_file, first_name, last_name, POLICY_LABEL, ALLOWED_POLICY_MIMES
        )
        if rec:
            policy_docs.append(rec)

    for f in car_documents or []:
        rec = await _store_upload(
            bucket, f, first_name, last_name, CAR_DOCS_LABEL, ALLOWED_DOC_MIMES
        )
        if rec:
            car_docs_ids.append(rec)

    for f in person_documents or []:
        rec = await _store_upload(
            bucket, f, first_name, last_name, PERSON_DOCS_LABEL, ALLOWED_DOC_MIMES
        )
        if rec:
            person_docs_ids.append(rec)

    record = {
        "name": name.strip(),
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "phone": phone.strip(),
        "car_name": car_name.strip(),
        "plate_number": plate_number.strip(),
        "vin_number": vin_number.strip(),
        "documents": policy_docs,
        "car_docs_ids": car_docs_ids,
        "person_docs_ids": person_docs_ids,
        "insurances": [
            {
                "insurance_start": start_dt,
                "insurance_end": end_dt,
                "created_at": datetime.utcnow(),
            }
        ],
        "created_at": datetime.utcnow(),
        "created_by": user.get("email"),
    }

    await records_col.insert_one(record)
    return RedirectResponse("/", status_code=303)


# ---------- DOWNLOAD FILE ----------
@app.get("/download_file/{file_id}")
async def download_file(file_id: str, user: dict = Depends(require_user)):
    bucket: AsyncIOMotorGridFSBucket = app.state.fs_bucket
    try:
        stream = await bucket.open_download_stream(ObjectId(file_id))
        filename = getattr(stream, "filename", None) or f"file_{file_id}"
        content_type = (
            (stream.metadata or {}).get("type") if stream.metadata else None
        ) or "application/octet-stream"
        data = await stream.read()
        return StreamingResponse(
            BytesIO(data),
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )
    except Exception as e:
        print("File download error:", e)
        raise HTTPException(status_code=404, detail="File not found")


# ---------- EXPORT SELECTED TO CSV ----------
@app.post("/export_selected_csv")
async def export_selected_csv(
    selected_ids: str = Form(...),
    user: dict = Depends(require_user),
):
    try:
        ids = [ObjectId(i) for i in selected_ids.split(",") if i.strip()]
        if not ids:
            raise HTTPException(status_code=400, detail="No records selected")

        cursor = records_col.find({"_id": {"$in": ids}})
        data = await cursor.to_list(length=None)

        out = StringIO()
        writer = csv.writer(out)
        writer.writerow(["Name", "Phone", "Car", "Plate", "VIN", "Start", "End"])
        for d in data:
            ins = d.get("insurances", [{}])[-1]
            writer.writerow(
                [
                    d.get("name", ""),
                    d.get("phone", ""),
                    d.get("car_name", ""),
                    d.get("plate_number", ""),
                    d.get("vin_number", ""),
                    ins.get("insurance_start").strftime("%Y-%m-%d")
                    if ins.get("insurance_start")
                    else "",
                    ins.get("insurance_end").strftime("%Y-%m-%d")
                    if ins.get("insurance_end")
                    else "",
                ]
            )

        out.seek(0)
        headers = {
            "Content-Disposition": 'attachment; filename="selected_insurances.csv"'
        }
        return StreamingResponse(
            iter([out.getvalue()]), media_type="text/csv", headers=headers
        )

    except Exception as e:
        print("CSV export error:", e)
        raise HTTPException(status_code=500, detail=f"Error exporting CSV: {e}")


# ---------- IMPORT PDF (auto-extract data) ----------
@app.post("/import_pdf")
async def import_pdf(
    file: UploadFile = File(...),
    user: dict = Depends(require_user),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    content = await file.read()
    data = extract_insurance_data(content)
    if not data:
        raise HTTPException(status_code=400, detail="Could not extract data")

    return JSONResponse(
        {
            "success": True,
            "parsed_data": data,
            "filename": file.filename,
        }
    )


# ---------- ADMIN: trigger a test alert email (manual) ----------
@app.post("/admin/test-email")
@app.get("/admin/test-email")
async def admin_test_email(user: dict = Depends(require_user)):
    """Fires the expiration-alert email immediately so SendGrid wiring can be verified
    without waiting for the 07:00 Europe/Bucharest cron tick."""
    items = await _collect_expiring_items()
    if not items:
        # Send a synthetic item so the email channel itself can be verified.
        items = [
            {
                "name": "TEST RECORD",
                "car_name": "Test Vehicle",
                "plate_number": "TEST 00 TST",
                "insurance_end": (datetime.utcnow() + timedelta(days=3)).date(),
                "days_left": 3,
            }
        ]
    try:
        send_email_alert(items)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"send failed: {e}")
    return JSONResponse(
        {
            "success": True,
            "sent_records": len(items),
            "requested_by": user.get("email"),
        }
    )


# ---------- ADMIN: internal-token endpoint for external schedulers ----------
@app.post("/admin/run-expiration-check")
async def admin_run_expiration_check(request: Request):
    """Same job the cron runs, exposed so an external scheduler / webhook can call it.
    Bypasses login via the X-Internal-Token shared-secret header configured in
    INTERNAL_API_TOKEN; the auth middleware enforces that check before we get here."""
    items = await _collect_expiring_items()
    if items:
        send_email_alert(items)
    return {"success": True, "sent_records": len(items)}
