"""File-naming and MIME helpers for uploaded documents."""

from __future__ import annotations

import os
import re
import unicodedata


# Document categories — value is the suffix used in the renamed filename.
POLICY_LABEL = "Polita"
CAR_DOCS_LABEL = "Documente Auto"
PERSON_DOCS_LABEL = "Documente Persoana"

# Anything not in this set gets squashed to a hyphen. Limit aggressively so the result is
# safe across Windows, macOS, Linux and URL-encoded download links.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9 _\-]")
_MULTI_DASH = re.compile(r"-{2,}")
_MULTI_SPACE = re.compile(r"\s{2,}")

ALLOWED_POLICY_MIMES = {"application/pdf"}
ALLOWED_DOC_MIMES = {
    "application/pdf",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/heic",
}


def sanitize_name_part(s: str) -> str:
    """Normalize a single name token (e.g. first name or last name) into a filesystem-
    and URL-safe slug while keeping it readable."""
    if not s:
        return ""
    # Romanian-specific replacements first; everything else strips combining marks.
    s = (
        s.replace("ș", "s").replace("Ș", "S")
         .replace("ț", "t").replace("Ț", "T")
         .replace("ş", "s").replace("Ş", "S")
         .replace("ţ", "t").replace("Ţ", "T")
    )
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _SAFE_CHARS.sub("-", s)
    s = _MULTI_DASH.sub("-", s).strip(" -_")
    s = _MULTI_SPACE.sub(" ", s)
    return s


def _ext_from_filename(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lstrip(".").lower()


def _ext_from_content_type(content_type: str) -> str:
    table = {
        "application/pdf": "pdf",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/heic": "heic",
    }
    return table.get((content_type or "").lower(), "")


def pick_extension(filename: str, content_type: str, default: str = "bin") -> str:
    return (
        _ext_from_filename(filename)
        or _ext_from_content_type(content_type)
        or default
    )


def build_filename(first_name: str, last_name: str, label: str, extension: str) -> str:
    """Compose the renamed filename used in GridFS. Falls back to `Document` if names
    are missing so we never produce ` - Polita.pdf` with a stray dash at the front."""
    first = sanitize_name_part(first_name)
    last = sanitize_name_part(last_name)
    if first or last:
        person = _MULTI_SPACE.sub(" ", f"{first} {last}").strip()
    else:
        person = "Document"
    extension = (extension or "bin").lower().lstrip(".")
    return f"{person} - {label}.{extension}"


def validate_mime(content_type: str, allowed: set[str]) -> bool:
    return (content_type or "").lower() in allowed
