import re
from fastapi import HTTPException

def validate_phone(phone: str):
    pattern = r"^\+40\d{9}$"
    if not re.match(pattern, phone):
        raise HTTPException(status_code=400, detail="Invalid Romanian phone number format (+40xxxxxxxxx).")

def validate_plate(plate: str):
    pattern = r"^[A-Z]{1,2}\s?\d{2}\s?[A-Z]{3}$"
    if not re.match(pattern, plate):
        raise HTTPException(status_code=400, detail="Invalid Romanian plate number format (e.g. IS 12 ABC).")

def validate_vin(vin: str):
    pattern = r"^[A-HJ-NPR-Z0-9]{17}$"
    if not re.match(pattern, vin):
        raise HTTPException(status_code=400, detail="Invalid VIN number (must be 17 characters, no I/O/Q).")
