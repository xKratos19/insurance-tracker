import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017/insurance")

client = AsyncIOMotorClient(MONGO_URI)
db = client.get_default_database()  # "insurance" if using URI above
records_col = db["insurance_records"]
fs_bucket = None  # set in main at startup (GridFSBucket)
