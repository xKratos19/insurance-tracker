from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
from bson import ObjectId
from gridfs import GridFSBucket
from io import BytesIO, StringIO
import csv, traceback, re, fitz

from .database import db, records_col
from .email_alert import send_email_alert
from .validators import validate_phone, validate_plate, validate_vin

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")


# ========== STARTUP ==========
@app.on_event("startup")
async def startup_event():
    app.state.fs_bucket = GridFSBucket(db.delegate)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_expiring_insurances, "cron", hour=7, timezone="Europe/Bucharest")
    scheduler.start()


# ========== CRON JOB ==========
async def check_expiring_insurances():
    upcoming = datetime.utcnow() + timedelta(days=7)
    cursor = records_col.find({"insurances.insurance_end": {"$lte": upcoming}})
    items = await cursor.to_list(length=None)
    if items:
        send_email_alert(items)


# ========== ROUTES ==========

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    cursor = records_col.find().sort("created_at", -1)
    data = await cursor.to_list(length=None)
    items = []

    for d in data:
        latest_ins = d.get("insurances", [{}])[-1] if d.get("insurances") else {}
        end = latest_ins.get("insurance_end")
        days_left = (end.date() - datetime.utcnow().date()).days if end else None

        items.append({
            "id": str(d["_id"]),
            "name": d.get("name"),
            "phone": d.get("phone"),
            "car_name": d.get("car_name"),
            "plate_number": d.get("plate_number"),
            "vin_number": d.get("vin_number"),
            "insurance_start": latest_ins.get("insurance_start").date() if latest_ins.get("insurance_start") else "",
            "insurance_end": end.date() if end else "",
            "days_left": days_left,
            "documents": d.get("documents", []),
        })

    return templates.TemplateResponse("index.html", {"request": request, "items": items, "today": datetime.utcnow().date()})


# ---------- ADD NEW RECORD ----------
@app.post("/add")
async def add_record(
    name: str = Form(...),
    phone: str = Form(...),
    car_name: str = Form(...),
    plate_number: str = Form(...),
    vin_number: str = Form(...),
    insurance_start: str = Form(...),
    insurance_end: str = Form(...),
    files: list[UploadFile] = File(None),
):
    validate_phone(phone)
    validate_plate(plate_number)
    validate_vin(vin_number)

    start_dt = datetime.strptime(insurance_start, "%Y-%m-%d")
    end_dt = datetime.strptime(insurance_end, "%Y-%m-%d")

    bucket = app.state.fs_bucket
    uploaded_docs = []

    if files:
        for f in files:
            content = await f.read()
            file_id = await bucket.upload_from_stream(f.filename, content, metadata={"type": f.content_type})
            uploaded_docs.append({
                "file_id": file_id,
                "filename": f.filename,
                "content_type": f.content_type,
                "uploaded_at": datetime.utcnow()
            })

    record = {
        "name": name.strip(),
        "phone": phone.strip(),
        "car_name": car_name.strip(),
        "plate_number": plate_number.strip(),
        "vin_number": vin_number.strip(),
        "documents": uploaded_docs,
        "insurances": [{
            "insurance_start": start_dt,
            "insurance_end": end_dt,
            "created_at": datetime.utcnow()
        }],
        "created_at": datetime.utcnow()
    }

    await records_col.insert_one(record)
    return RedirectResponse("/", status_code=303)


# ---------- DOWNLOAD FILE ----------
@app.get("/download_file/{file_id}")
async def download_file(file_id: str):
    bucket = app.state.fs_bucket
    buffer = BytesIO()
    try:
        await bucket.download_to_stream(ObjectId(file_id), buffer)
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="application/octet-stream",
                                 headers={"Content-Disposition": f"attachment; filename=file_{file_id}.pdf"})
    except Exception as e:
        print("File download error:", e)
        raise HTTPException(status_code=404, detail="File not found")


# ---------- EXPORT SELECTED TO CSV ----------
@app.post("/export_selected_csv")
async def export_selected_csv(selected_ids: str = Form(...)):
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
            writer.writerow([
                d.get("name", ""),
                d.get("phone", ""),
                d.get("car_name", ""),
                d.get("plate_number", ""),
                d.get("vin_number", ""),
                ins.get("insurance_start").strftime("%Y-%m-%d") if ins.get("insurance_start") else "",
                ins.get("insurance_end").strftime("%Y-%m-%d") if ins.get("insurance_end") else "",
            ])

        out.seek(0)
        headers = {"Content-Disposition": 'attachment; filename="selected_insurances.csv"'}
        return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers=headers)

    except Exception as e:
        print("CSV export error:", e)
        raise HTTPException(status_code=500, detail=f"Error exporting CSV: {e}")


# ---------- IMPORT PDF (auto-extract data) ----------
@app.post("/import_pdf")
async def import_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files allowed")

    content = await file.read()
    data = extract_insurance_data(content)
    if not data:
        raise HTTPException(status_code=400, detail="Could not extract data")

    return JSONResponse({
        "success": True,
        "parsed_data": data,
        "filename": file.filename
    })


def extract_insurance_data(pdf_bytes: bytes):
    """
    Robust extractor for: name, VIN, plate, start_date, end_date
    Works across multiple Romanian policy layouts (multi-column, different labels).
    """
    import fitz, re
    from datetime import datetime

    # --- helpers -------------------------------------------------------------
    def norm_ws(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip()

    def to_iso(d: str) -> str:
        for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(d, fmt).strftime("%Y-%m-%d")
            except:  # noqa: E722
                pass
        return ""

    def normalize_plate(p: str) -> str:
        p = re.sub(r"[^A-Z0-9]", "", p.upper())
        # Romanian formats: B 99 ABC or AA 99 ABC
        # Try to re-space nicely
        if len(p) in (7, 8):  # common lengths after removing spaces
            # Heuristic: if starts with B (one-letter county)
            if p.startswith("B"):
                # B + 2/3 digits + 3 letters
                m = re.match(r"^B(\d{2,3})([A-Z]{3})$", p)
                if m:
                    return f"B {m.group(1)} {m.group(2)}"
            # two-letter county
            m = re.match(r"^([A-Z]{2})(\d{2,3})([A-Z]{3})$", p)
            if m:
                return f"{m.group(1)} {m.group(2)} {m.group(3)}"
        return p  # fallback

    # --- read PDF as ordered blocks -----------------------------------------
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            blocks_all = []
            for page in doc:
                blocks = page.get_text("blocks")  # (x0,y0,x1,y1,text, block_no, ...)
                # sort by y then x to reconstruct reading order
                blocks = sorted(blocks, key=lambda b: (round(b[1]), round(b[0])))
                blocks_all.extend([norm_ws(b[4]) for b in blocks if b[4].strip()])
            text = "\n".join(blocks_all)
    except Exception as e:
        print("PDF parse error:", e)
        return {}

    text_flat = norm_ws(text)
    text_lc = text_flat.lower()

    # --- search helpers (label proximity) ------------------------------------
    def find_after(labels, max_chars=120):
        """
        Find the first occurrence of any label and return up to max_chars after it.
        """
        for lab in labels:
            i = text_lc.find(lab.lower())
            if i != -1:
                seg = text_flat[i : i + len(lab) + max_chars]
                return seg
        return ""

    # --- VIN (global, very reliable) -----------------------------------------
    # VIN is 17 chars, excludes I,O,Q
    vin = ""
    vin_match = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", text_flat)
    if vin_match:
        vin = vin_match.group(1)

    # If not found, look near likely labels
    if not vin:
        near_vin = find_after(["VIN", "Serie șasiu", "Serie sasiu", "Serie CIV", "Serie"])
        m = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", near_vin)
        vin = m.group(1) if m else ""

    # --- Plate (global + normalized) -----------------------------------------
    plate = ""
    # AA 99 AAA or B 99 AAA (spaces optional in PDF)
    plate_pat = r"\b((?:[A-Z]{2}\s?\d{2,3}\s?[A-Z]{3})|(?:B\s?\d{2,3}\s?[A-Z]{3}))\b"
    m = re.search(plate_pat, text_flat)
    if not m:
        # look near labels
        near_plate = find_after(
            ["nr. înmatriculare", "nr inmatriculare", "număr înmatriculare",
             "numar inmatriculare", "înregistrare", "inregistrare"], max_chars=80
        )
        m = re.search(plate_pat, near_plate)
    if m:
        plate = normalize_plate(m.group(1))

    # --- Dates: prefer "de la ... / până la ..." context ---------------------
    start, end = "", ""

    # Capture within same small window
    window = find_after(["valabilitate contract", "perioada de asigurare", "valabilitate"], max_chars=200)
    m1 = re.search(r"de la\s*(\d{2}[./-]\d{2}[./-]\d{4})", window, flags=re.IGNORECASE)
    m2 = re.search(r"p[aă]n[ăa]\s*la\s*(\d{2}[./-]\d{2}[./-]\d{4})", window, flags=re.IGNORECASE)
    if m1:
        start = to_iso(m1.group(1))
    if m2:
        end = to_iso(m2.group(1))

    # Fallback: choose earliest as start, latest as end
    if not start or not end:
        all_dates = re.findall(r"(\d{2}[./-]\d{2}[./-]\d{4})", text_flat)
        parsed = []
        for d in all_dates:
            iso = to_iso(d)
            if iso:
                parsed.append(datetime.strptime(iso, "%Y-%m-%d"))
        parsed = sorted(set(parsed))
        if parsed:
            if not start:
                start = parsed[0].strftime("%Y-%m-%d")
            if not end and len(parsed) > 1:
                # choose the farthest in future from start
                end = parsed[-1].strftime("%Y-%m-%d")

    # --- Name: search near common labels, prefer ALLCAPS tokens --------------
    name = ""
    near_name = find_after(
        ["asigurat", "proprietar", "utilizator", "asigurat proprietar"], max_chars=120
    )
    # Strategy: pick 2–4 consecutive uppercase words (with diacritics allowed)
    cap_word = r"[A-ZĂÂÎȘȚ][A-ZĂÂÎȘȚ\-']+"
    m = re.search(rf"({cap_word}(?:\s+{cap_word}){{1,3}})", near_name)
    if m:
        name = m.group(1)
    else:
        # secondary: generic full name pattern anywhere
        m2 = re.search(rf"\b({cap_word}\s+{cap_word}(?:\s+{cap_word})?)\b", text_flat)
        if m2:
            name = m2.group(1)

    # Final cleanups
    name = name.strip()
    vin = vin.strip()
    plate = plate.strip()

    return {
        "name": name,
        "vin_number": vin,
        "plate_number": plate,
        "insurance_start": start,
        "insurance_end": end,
    }