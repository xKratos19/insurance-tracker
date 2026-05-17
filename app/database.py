import os
from urllib.parse import urlsplit

import certifi
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongo:27017/insurance")
# Explicit override always wins; otherwise we parse the path out of the URI; finally we
# fall back to "insurance" so the app boots even if the URI lacks a default database.
MONGO_DB = os.getenv("MONGO_DB", "").strip()

# Atlas (`mongodb+srv://`) requires TLS. Pin the CA bundle to certifi's so we don't
# depend on the container's system trust store being current.
_client_kwargs: dict = {"serverSelectionTimeoutMS": 20000}
if MONGO_URI.startswith("mongodb+srv://") or "tls=true" in MONGO_URI.lower():
    _client_kwargs["tlsCAFile"] = certifi.where()

client = AsyncIOMotorClient(MONGO_URI, **_client_kwargs)


def _db_name_from_uri(uri: str) -> str:
    # urlsplit handles both `mongodb://` and `mongodb+srv://`. The path is "/dbname".
    path = urlsplit(uri).path or ""
    return path.lstrip("/").split("?", 1)[0] or ""


db_name = MONGO_DB or _db_name_from_uri(MONGO_URI) or "insurance"
db = client[db_name]
records_col = db["insurance_records"]
audit_col = db["audit_logs"]
fs_bucket = None  # set in main at startup (GridFSBucket)
