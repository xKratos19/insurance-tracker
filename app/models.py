from sqlalchemy import Column, Integer, String, Date
from .database import Base

class Insurance(Base):
    __tablename__ = "insurance_records"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    car_name = Column(String, nullable=False)
    plate_number = Column(String, nullable=False)
    vin_number = Column(String, nullable=False)
    insurance_start = Column(Date, nullable=False)
    insurance_end = Column(Date, nullable=False)
    pdf_path = Column(String, nullable=True)
