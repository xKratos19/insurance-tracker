from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Path
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
from bson import ObjectId
from io import BytesIO, StringIO
from pytz import timezone

from motor.motor_asyncio import AsyncIOMotorGridFSBucket  # ✅ async GridFS
import csv
import traceback

from .database import db, records_col, audit_col
from .email_alert import send_email_alert
from .validators import validate_phone, validate_plate, validate_vin

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

# ---------- JOB ----------
async def check_expiring_insurances():
    upcoming = datetime.utcnow() + timedelta(days=7)
    cursor = records_col.find({"insurance_end": {"$lte": upcoming}})
    items = await cursor.to_list(length=None)
    if items:
        send_email_alert(items)

# ---------- STARTUP ----------
@app.on_event("startup")
async def startup_event():
    app.state.fs_bucket = AsyncIOMotorGridFSBucket(db)
    scheduler = AsyncIOScheduler(timezone=timezone("Europe/Bucharest"))
    scheduler.add_job(check_expiring_insurances, "cron", hour=7)
    scheduler.start()
    app.state.scheduler = scheduler

# ---------- AUDIT UTILITY ----------
async def log_audit(action: str, record_id: str, changes: dict = None, old_data: dict = None):
    """Store an audit entry in MongoDB"""
    entry = {
        "action": action,
        "record_id": record_id,
        "timestamp": datetime.utcnow(),
        "changes": changes or {},
        "old_data": old_data or {},
    }
    await audit_col.insert_one(entry)

# ---------- HOME ----------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    cursor = records_col.find().sort("insurance_end", 1)
    data = await cursor.to_list(length=None)
    items = []
    for d in data:
        end = d.get("insurance_end")
        days_left = (end.date() - datetime.utcnow().date()).days if end else None
        items.append({
            "id": str(d["_id"]),
            "name": d.get("name"),
            "phone": d.get("phone"),
            "car_name": d.get("car_name"),
            "plate_number": d.get("plate_number"),
            "vin_number": d.get("vin_number"),
            "insurance_start": d.get("insurance_start").date() if d.get("insurance_start") else "",
            "insurance_end": end.date() if end else "",
            "days_left": days_left,
            "pdf_id": str(d["pdf_id"]) if d.get("pdf_id") else None,
        })
    return templates.TemplateResponse("index.html", {"request": request, "items": items, "today": datetime.utcnow().date()})

# ---------- ADD ----------
@app.post("/add")
async def add_record(
    name: str = Form(...),
    phone: str = Form(...),
    car_name: str = Form(...),
    plate_number: str = Form(...),
    vin_number: str = Form(...),
    insurance_start: str = Form(...),
    insurance_end: str = Form(...),
    pdf: UploadFile = File(None),
):
    validate_phone(phone)
    validate_plate(plate_number)
    validate_vin(vin_number)

    start_dt = datetime.strptime(insurance_start, "%Y-%m-%d")
    end_dt = datetime.strptime(insurance_end, "%Y-%m-%d")

    pdf_id = None
    if pdf and pdf.filename and pdf.content_type == "application/pdf":
        body = await pdf.read()
        stream = BytesIO(body)
        pdf_id = await app.state.fs_bucket.upload_from_stream(pdf.filename, stream)

    doc = {
        "name": name.strip(),
        "phone": phone.strip(),
        "car_name": car_name.strip(),
        "plate_number": plate_number.strip(),
        "vin_number": vin_number.strip(),
        "insurance_start": start_dt,
        "insurance_end": end_dt,
        "pdf_id": pdf_id,
    }
    result = await records_col.insert_one(doc)

    # ✅ Log audit
    await log_audit("create", str(result.inserted_id), changes=doc)

    return RedirectResponse("/", status_code=303)

# ---------- EDIT ----------
@app.get("/edit/{record_id}", response_class=HTMLResponse)
async def edit_record(request: Request, record_id: str = Path(...)):
    rec = await records_col.find_one({"_id": ObjectId(record_id)})
    if not rec:
        raise HTTPException(status_code=404, detail="Record not found")
    record_data = {
        "id": str(rec["_id"]),
        "name": rec.get("name", ""),
        "phone": rec.get("phone", ""),
        "car_name": rec.get("car_name", ""),
        "plate_number": rec.get("plate_number", ""),
        "vin_number": rec.get("vin_number", ""),
        "insurance_start": rec.get("insurance_start").strftime("%Y-%m-%d") if rec.get("insurance_start") else "",
        "insurance_end": rec.get("insurance_end").strftime("%Y-%m-%d") if rec.get("insurance_end") else "",
    }
    return templates.TemplateResponse("edit.html", {"request": request, "record": record_data})

# ---------- UPDATE ----------
@app.post("/update/{record_id}")
async def update_record(
    record_id: str,
    name: str = Form(...),
    phone: str = Form(...),
    car_name: str = Form(...),
    plate_number: str = Form(...),
    vin_number: str = Form(...),
    insurance_start: str = Form(...),
    insurance_end: str = Form(...),
    pdf: UploadFile = File(None),
):
    try:
        validate_phone(phone)
        validate_plate(plate_number)
        validate_vin(vin_number)

        start_dt = datetime.strptime(insurance_start, "%Y-%m-%d")
        end_dt = datetime.strptime(insurance_end, "%Y-%m-%d")

        update_data = {
            "name": name.strip(),
            "phone": phone.strip(),
            "car_name": car_name.strip(),
            "plate_number": plate_number.strip(),
            "vin_number": vin_number.strip(),
            "insurance_start": start_dt,
            "insurance_end": end_dt,
        }

        old = await records_col.find_one({"_id": ObjectId(record_id)})

        if pdf and pdf.filename and pdf.content_type == "application/pdf":
            body = await pdf.read()
            stream = BytesIO(body)
            new_pdf_id = await app.state.fs_bucket.upload_from_stream(pdf.filename, stream)
            if old and old.get("pdf_id"):
                try:
                    await app.state.fs_bucket.delete(ObjectId(old["pdf_id"]))
                except Exception as e:
                    print("Warn: failed to delete old PDF:", e)
            update_data["pdf_id"] = new_pdf_id

        res = await records_col.update_one({"_id": ObjectId(record_id)}, {"$set": update_data})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail="Record not found")

        # ✅ Log audit
        await log_audit("update", record_id, changes=update_data, old_data=old)

        return RedirectResponse("/", status_code=303)

    except Exception as e:
        print("❌ Error updating record:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error updating record: {e}")

# ---------- DOWNLOAD ----------
@app.get("/download/{record_id}")
async def download_pdf(record_id: str):
    rec = await records_col.find_one({"_id": ObjectId(record_id)})
    if not rec or not rec.get("pdf_id"):
        raise HTTPException(status_code=404, detail="PDF not found")

    file_id = rec["pdf_id"] if isinstance(rec["pdf_id"], ObjectId) else ObjectId(rec["pdf_id"])
    buf = BytesIO()
    await app.state.fs_bucket.download_to_stream(file_id, buf)
    buf.seek(0)
    filename = f'{rec.get("plate_number","insurance")}.pdf'
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(buf, media_type="application/pdf", headers=headers)

# ---------- DELETE ----------
@app.post("/delete/{record_id}")
async def delete_record(record_id: str):
    try:
        # Safely convert the ID to ObjectId
        try:
            obj_id = ObjectId(record_id)
        except InvalidId:
            raise HTTPException(status_code=400, detail="Invalid record ID")

        rec = await records_col.find_one({"_id": obj_id})
        if not rec:
            raise HTTPException(status_code=404, detail="Record not found")

        # Delete PDF if attached
        if rec.get("pdf_id"):
            try:
                await app.state.fs_bucket.delete(ObjectId(rec["pdf_id"]))
            except Exception as e:
                print("⚠️ PDF deletion warning:", e)

        # Delete record
        await records_col.delete_one({"_id": obj_id})

        # Log audit
        await log_audit("delete", record_id, old_data=rec)

        print(f"✅ Deleted record {record_id}")
        return RedirectResponse("/", status_code=303)

    except Exception as e:
        print("❌ Delete error:", e)
        raise HTTPException(status_code=500, detail=f"Error deleting record: {e}")

# ---------- JOBS ----------
@app.get("/jobs")
async def list_jobs():
    scheduler = getattr(app.state, "scheduler", None)
    if not scheduler:
        raise HTTPException(status_code=500, detail="Scheduler not initialized")
    return [{"id": j.id, "next_run": str(j.next_run_time)} for j in scheduler.get_jobs()]

@app.get("/audit", response_class=HTMLResponse)
async def view_audit_logs(request: Request):
    cursor = audit_col.find().sort("timestamp", -1).limit(100)
    logs = await cursor.to_list(length=None)

    formatted = []
    for log in logs:
        formatted.append({
            "id": str(log["_id"]),
            "action": log.get("action", "unknown").capitalize(),
            "record_id": log.get("record_id", ""),
            "timestamp": log.get("timestamp").strftime("%Y-%m-%d %H:%M:%S") if log.get("timestamp") else "",
            "changes": ", ".join(log.get("changes", {}).keys()) if isinstance(log.get("changes"), dict) else str(log.get("changes")),
        })

    return templates.TemplateResponse("audit.html", {"request": request, "logs": formatted})

@app.post("/export_selected_csv")
async def export_selected_csv(selected_ids: str = Form(...)):
    try:
        ids = [ObjectId(i) for i in selected_ids.split(",") if i.strip()]
        if not ids:
            raise HTTPException(status_code=400, detail="No records selected for export")

        cursor = records_col.find({"_id": {"$in": ids}})
        data = await cursor.to_list(length=None)

        if not data:
            raise HTTPException(status_code=404, detail="No records found for those IDs")

        # Build CSV
        out = StringIO()
        writer = csv.writer(out)
        writer.writerow(["Name", "Phone", "Car", "Plate", "VIN", "Start", "End"])
        for d in data:
            writer.writerow([
                d.get("name", ""),
                d.get("phone", ""),
                d.get("car_name", ""),
                d.get("plate_number", ""),
                d.get("vin_number", ""),
                d.get("insurance_start").strftime("%Y-%m-%d") if d.get("insurance_start") else "",
                d.get("insurance_end").strftime("%Y-%m-%d") if d.get("insurance_end") else "",
            ])
        out.seek(0)

        headers = {"Content-Disposition": 'attachment; filename="selected_insurances.csv"'}
        return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers=headers)

    except Exception as e:
        print("❌ CSV export error:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error exporting CSV: {e}")