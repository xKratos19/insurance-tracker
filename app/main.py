from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
from bson import ObjectId
from gridfs import GridFSBucket
from io import BytesIO, StringIO
import csv

from .database import db, records_col
from .email_alert import send_email_alert
from .validators import validate_phone, validate_plate, validate_vin

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

@app.on_event("startup")
async def startup_event():
    app.state.fs_bucket = GridFSBucket(db.delegate)  # <-- FIX HERE

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_expiring_insurances, "cron", hour=9)
    scheduler.start()

async def check_expiring_insurances():
    upcoming = datetime.utcnow() + timedelta(days=7)
    cursor = records_col.find({"insurance_end": {"$lte": upcoming}})
    items = await cursor.to_list(length=None)
    if items:
        send_email_alert(items)

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
    # Validation
    validate_phone(phone)
    validate_plate(plate_number)
    validate_vin(vin_number)

    start_dt = datetime.strptime(insurance_start, "%Y-%m-%d")
    end_dt = datetime.strptime(insurance_end, "%Y-%m-%d")

    pdf_id = None
    if pdf and pdf.filename and pdf.content_type == "application/pdf":
        blob = await pdf.read()
        pdf_id = app.state.fs_bucket.upload_from_stream(pdf.filename, BytesIO(blob))

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
    await records_col.insert_one(doc)
    return RedirectResponse("/", status_code=303)

@app.post("/export_selected_csv")
async def export_selected_csv(selected_ids: str = Form(...)):
    ids = [ObjectId(i) for i in selected_ids.split(",") if i]
    cursor = records_col.find({"_id": {"$in": ids}})
    data = await cursor.to_list(length=None)

    out = StringIO()
    w = csv.writer(out)
    w.writerow(["Name", "Phone", "Car Name", "Plate Number", "VIN", "Start", "End"])
    for d in data:
        w.writerow([
            d.get("name"), d.get("phone"), d.get("car_name"),
            d.get("plate_number"), d.get("vin_number"),
            d.get("insurance_start").date() if d.get("insurance_start") else "",
            d.get("insurance_end").date() if d.get("insurance_end") else "",
        ])
    out.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="selected_insurances.csv"'}
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers=headers)
