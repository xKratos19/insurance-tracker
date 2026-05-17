import os

import certifi
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017/insurance")

# Atlas (`mongodb+srv://`) requires TLS. Pin the CA bundle to certifi's so we don't
# depend on the container's system trust store being current.
_client_kwargs: dict = {"serverSelectionTimeoutMS": 20000}
if MONGO_URI.startswith("mongodb+srv://") or "tls=true" in MONGO_URI.lower():
    _client_kwargs["tlsCAFile"] = certifi.where()

client = AsyncIOMotorClient(MONGO_URI, **_client_kwargs)
db = client.get_default_database()  # "insurance" if using URI above
records_col = db["insurance_records"]
audit_col = db["audit_logs"]
fs_bucket = None  # set in main at startup (GridFSBucket)
