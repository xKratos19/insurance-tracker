from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from datetime import date, timedelta
from .database import Base, engine, SessionLocal
from .models import Insurance
from apscheduler.schedulers.background import BackgroundScheduler
from .email_alert import send_email_alert
import shutil, os

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/uploads", StaticFiles(directory="app/uploads"), name="uploads")

Base.metadata.create_all(bind=engine)

def check_expiring_insurances():
    db = SessionLocal()
    today = date.today()
    in_7_days = today + timedelta(days=7)
    expiring = db.query(Insurance).filter(Insurance.insurance_end <= in_7_days).all()
    if expiring:
        send_email_alert(expiring)
    db.close()

scheduler = BackgroundScheduler()
scheduler.add_job(check_expiring_insurances, 'interval', days=1)
scheduler.start()

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    db = SessionLocal()
    records = db.query(Insurance).all()
    db.close()
    return templates.TemplateResponse("index.html", {"request": request, "records": records, "today": date.today()})

@app.post("/add")
async def add_record(
    name: str = Form(...),
    phone: str = Form(...),
    car_name: str = Form(...),
    plate_number: str = Form(...),
    vin_number: str = Form(...),
    insurance_start: str = Form(...),
    insurance_end: str = Form(...),
    pdf: UploadFile = File(None)
):
    db = SessionLocal()
    pdf_path = None
    if pdf:
        os.makedirs("app/uploads", exist_ok=True)
        pdf_path = f"app/uploads/{pdf.filename}"
        with open(pdf_path, "wb") as buffer:
            shutil.copyfileobj(pdf.file, buffer)
    record = Insurance(
        name=name,
        phone=phone,
        car_name=car_name,
        plate_number=plate_number,
        vin_number=vin_number,
        insurance_start=insurance_start,
        insurance_end=insurance_end,
        pdf_path=pdf_path
    )
    db.add(record)
    db.commit()
    db.close()
    return RedirectResponse("/", status_code=303)

@app.get("/download/{record_id}")
def download_file(record_id: int):
    db = SessionLocal()
    record = db.query(Insurance).get(record_id)
    db.close()
    if record and record.pdf_path:
        return FileResponse(record.pdf_path, filename=os.path.basename(record.pdf_path))
    return {"error": "File not found"}
