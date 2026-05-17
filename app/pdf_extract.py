"""
Romanian insurance PDF text extractor.

Targets multiple insurers (Euroins, Groupama, Allianz, Axeria, Grawe, Omniasig) and
gracefully degrades when labels are missing or laid out across columns.

Public surface:
    extract_insurance_data(pdf_bytes: bytes) -> dict with keys:
        name, first_name, last_name, vin_number, plate_number,
        insurance_start, insurance_end, issuer
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Iterable, Optional

import fitz


# ---------------------------------------------------------------------------- #
# Constants / patterns
# ---------------------------------------------------------------------------- #

DATE_PAT = re.compile(r"\b(\d{2})[./\-](\d{2})[./\-](\d{4})\b")
VIN_PAT = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
# Romanian plate: 1 or 2 county letters + 2-3 digits + 3 letters. Bucharest = B.
PLATE_PAT = re.compile(
    r"\b((?:B|[A-Z]{2})\s?\d{2,3}\s?[A-Z]{3})\b"
)

ISSUER_KEYWORDS = {
    "euroins": "Euroins",
    "groupama": "Groupama",
    "allianz": "Allianz",
    "țiriac": "Allianz-Țiriac",
    "tiriac": "Allianz-Țiriac",
    "axeria": "Axeria",
    "grawe": "Grawe",
    "omniasig": "Omniasig",
    "asirom": "Asirom",
    "city insurance": "City Insurance",
    "generali": "Generali",
    "uniqa": "Uniqa",
}

NAME_LABELS = (
    "asigurat proprietar",
    "asigurat / proprietar",
    "nume si prenume",
    "nume și prenume",
    "nume / prenume",
    "asigurat",
    "proprietar",
    "utilizator",
    "contractant",
    "asiguratul",
)
FIRST_NAME_LABELS = ("prenume", "prenumele")
LAST_NAME_LABELS = ("nume", "numele")

START_LABELS = (
    "valabil de la",
    "valabilitate de la",
    "începere",
    "data început",
    "data inceput",
    "perioada de asigurare de la",
    "de la data de",
    "de la",
)
END_LABELS = (
    "valabil pana la",
    "valabil până la",
    "valabilitate pana la",
    "valabilitate până la",
    "data sfârșit",
    "data sfarsit",
    "expirare",
    "expira la",
    "expiră la",
    "pana la",
    "până la",
)
ISSUE_LABELS = (
    "data emiterii",
    "data emitere",
    "emis la",
    "emisă la",
    "emis în data",
    "data contract",
)

# Tokens we never want to mistake for a person's name
NAME_BLOCKLIST = {
    "ASIGURAT", "PROPRIETAR", "UTILIZATOR", "CONTRACTANT",
    "POLITA", "POLIȚA", "POLITĂ", "CONTRACT", "ASIGURARE",
    "RCA", "CASCO", "PAD", "SOCIETATE", "COMPANIE", "SA", "SRL",
    "STR", "STRADA", "BLOC", "SCARA", "AP", "ETAJ", "JUD", "JUDET",
    "ROMANIA", "ROMÂNIA", "BUCUREȘTI", "BUCURESTI", "IAȘI", "IASI",
    "CLUJ", "TIMISOARA", "TIMIȘOARA", "CONSTANTA", "CONSTANȚA",
    "EUROINS", "GROUPAMA", "ALLIANZ", "TIRIAC", "ȚIRIAC",
    "AXERIA", "GRAWE", "OMNIASIG", "ASIROM", "GENERALI", "UNIQA",
    "ASIGURĂRI", "ASIGURARI", "MARCA", "MODEL", "AUTOTURISM",
    "FACTURA", "FACTURĂ", "TOTAL", "CHITANTA", "CHITANȚĂ",
}

# ---------------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------------- #

def _strip_diacritics(s: str) -> str:
    """Drop combining marks but keep underlying letters (Ș→S, ă→a, etc.)."""
    nfkd = unicodedata.normalize("NFKD", s)
    # Special-case Romanian s/t with comma below — they aren't combining marks in some
    # encodings; replace explicitly.
    nfkd = (
        nfkd.replace("ș", "s").replace("Ș", "S")
            .replace("ț", "t").replace("Ț", "T")
            .replace("ş", "s").replace("Ş", "S")
            .replace("ţ", "t").replace("Ţ", "T")
    )
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _safe_iso(d: str) -> str:
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(d, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _parse_date(d: str) -> Optional[date]:
    iso = _safe_iso(d)
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return None


def _normalize_plate(p: str) -> str:
    p = re.sub(r"[^A-Z0-9]", "", p.upper())
    if p.startswith("B"):
        m = re.match(r"^B(\d{2,3})([A-Z]{3})$", p)
        if m:
            return f"B {m.group(1)} {m.group(2)}"
    m = re.match(r"^([A-Z]{2})(\d{2,3})([A-Z]{3})$", p)
    if m:
        return f"{m.group(1)} {m.group(2)} {m.group(3)}"
    return p


def _read_pdf_blocks(pdf_bytes: bytes) -> tuple[str, list[str]]:
    """Read PDF, return (joined_text, ordered_lines). Sorted by y then x to keep
    multi-column layouts readable."""
    lines: list[str] = []
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                blocks = page.get_text("blocks") or []
                blocks = sorted(blocks, key=lambda b: (round(b[1] / 3), round(b[0])))
                for b in blocks:
                    raw = b[4] if len(b) > 4 else ""
                    for ln in raw.splitlines():
                        ln = _norm_ws(ln)
                        if ln:
                            lines.append(ln)
    except Exception as e:  # pragma: no cover — fitz errors vary by PDF
        print("PDF parse error:", e)
        return "", []
    return "\n".join(lines), lines


def _find_label_index(lines: list[str], labels: Iterable[str]) -> int:
    """Return the line index whose normalized text contains any label. -1 if absent."""
    haystacks = [_strip_diacritics(ln).lower() for ln in lines]
    for label in labels:
        needle = _strip_diacritics(label).lower()
        for i, h in enumerate(haystacks):
            if needle in h:
                return i
    return -1


def _value_after_label(line: str, labels: Iterable[str]) -> str:
    """If a label is present in `line`, return whatever comes after the first
    occurrence of any label (stripped of separators like ':')."""
    norm = _strip_diacritics(line).lower()
    for label in labels:
        needle = _strip_diacritics(label).lower()
        idx = norm.find(needle)
        if idx == -1:
            continue
        # Map back to original `line` index using the same offset (NFKD doesn't change
        # character count for ASCII-translatable diacritics — close enough for these
        # labels, which are pure Latin).
        tail = line[idx + len(needle):]
        return tail.lstrip(" :\t-—–").strip()
    return ""


# ---------------------------------------------------------------------------- #
# Field extractors
# ---------------------------------------------------------------------------- #

def _extract_dates(text: str, lines: list[str]) -> tuple[str, str]:
    """Pull policy start and end while ignoring the issuance date.

    Strategy:
      1. Look for explicit "de la ... până la ..." pairs (same window).
      2. Look at lines that contain start/end labels.
      3. Collect issuance dates and exclude them from the date pool.
      4. Fall back to (earliest, latest) of the remaining dates.
    """
    start = end = ""

    # 1. paired "de la ... până la ..."
    paired = re.search(
        r"(?:de\s+la|valabilitate\s+de\s+la|valabil\s+de\s+la)\s*[:\-]?\s*"
        r"(\d{2}[./\-]\d{2}[./\-]\d{4}).{0,80}?"
        r"(?:p[âa]n[ăa]\s+la|valabil\s+p[âa]n[ăa]\s+la|expirare|sf[âa]r[șs]it)\s*[:\-]?\s*"
        r"(\d{2}[./\-]\d{2}[./\-]\d{4})",
        _strip_diacritics(text),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if paired:
        start = _safe_iso(paired.group(1)) or start
        end = _safe_iso(paired.group(2)) or end

    # 2. label-anchored single dates (look on same line, next 2 lines if needed)
    def _by_labels(labels: Iterable[str]) -> str:
        idx = _find_label_index(lines, labels)
        if idx < 0:
            return ""
        for j in range(idx, min(idx + 3, len(lines))):
            for m in DATE_PAT.finditer(lines[j]):
                iso = _safe_iso(f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
                if iso:
                    return iso
        return ""

    if not start:
        start = _by_labels(START_LABELS)
    if not end:
        end = _by_labels(END_LABELS)

    # 3. collect issuance dates so we can exclude them from the fallback pool
    issuance: set[date] = set()
    issue_idx = _find_label_index(lines, ISSUE_LABELS)
    if issue_idx >= 0:
        for j in range(issue_idx, min(issue_idx + 2, len(lines))):
            for m in DATE_PAT.finditer(lines[j]):
                d = _parse_date(f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
                if d:
                    issuance.add(d)

    if start and end:
        return start, end

    # 4. fallback — earliest as start, farthest-future as end, excluding issuance dates
    candidates: list[date] = []
    for m in DATE_PAT.finditer(text):
        d = _parse_date(f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
        if d and d not in issuance:
            candidates.append(d)
    candidates = sorted(set(candidates))

    if not start and candidates:
        start = candidates[0].strftime("%Y-%m-%d")
    if not end and candidates:
        # Pick a date that's after `start` and >= ~30 days later (policies usually run
        # months or a year). Otherwise just take the last.
        start_d = _parse_date(start)
        future = [d for d in candidates if start_d and (d - start_d).days >= 25]
        end = (future[-1] if future else candidates[-1]).strftime("%Y-%m-%d")

    return start, end


def _looks_like_name_token(tok: str) -> bool:
    if not tok or len(tok) < 2:
        return False
    if tok.upper() in NAME_BLOCKLIST:
        return False
    if not re.match(r"^[A-ZĂÂÎȘȚȘȚŞŢ][A-ZĂÂÎȘȚȘȚŞŢa-zăâîșțşţ\-']+$", tok):
        return False
    return True


def _clean_name_value(raw: str) -> str:
    """Strip trailing CNP/digits/dots that often follow names on a single line."""
    # Drop anything from the first digit run onwards (CNP, ID numbers, etc.)
    raw = re.split(r"\d", raw, maxsplit=1)[0]
    raw = raw.strip(" :,;.\t-—–")
    # Keep up to 5 capitalised tokens
    tokens = [t for t in raw.split() if _looks_like_name_token(t)]
    return " ".join(tokens[:5])


def _extract_name(lines: list[str]) -> tuple[str, str, str]:
    """Return (full_name, first_name, last_name).

    Strategies, in order:
      (a) Explicit "Nume" / "Prenume" labels on the same line OR on the next line.
      (b) "Asigurat / Proprietar" combined label — value on same or next line.
      (c) Anywhere in the doc: two consecutive ALL-CAPS tokens not in blocklist.
    """
    first = last = ""

    # (a) split labels
    fi = _find_label_index(lines, FIRST_NAME_LABELS)
    li = _find_label_index(lines, LAST_NAME_LABELS)

    def _value_for(idx: int, labels: Iterable[str]) -> str:
        if idx < 0:
            return ""
        same_line = _clean_name_value(_value_after_label(lines[idx], labels))
        if same_line:
            return same_line
        # Try the next non-empty line — but bail if it looks like another label
        for k in range(idx + 1, min(idx + 3, len(lines))):
            cand = lines[k]
            norm = _strip_diacritics(cand).lower()
            if any(
                lab in norm
                for lab in (
                    "cnp", "adresa", "adresă", "telefon", "judet", "județ",
                    "localitate", "strada", "str.", "data", "polit",
                )
            ):
                return ""
            cleaned = _clean_name_value(cand)
            if cleaned:
                return cleaned
        return ""

    if li >= 0 and li != fi:
        last = _value_for(li, LAST_NAME_LABELS)
    if fi >= 0 and fi != li:
        first = _value_for(fi, FIRST_NAME_LABELS)

    # (b) combined "Asigurat / Proprietar / Contractant" — value is usually one full name
    if not (first and last):
        idx = _find_label_index(lines, NAME_LABELS)
        if idx >= 0:
            combined = _clean_name_value(_value_after_label(lines[idx], NAME_LABELS))
            if not combined:
                # value sits on the next line
                for k in range(idx + 1, min(idx + 3, len(lines))):
                    combined = _clean_name_value(lines[k])
                    if combined:
                        break
            if combined:
                parts = combined.split()
                # Romanian convention: surname(s) first, then given name(s).
                # If exactly two tokens, assume LAST FIRST.
                if not last and len(parts) >= 1:
                    last = parts[0]
                if not first and len(parts) >= 2:
                    first = " ".join(parts[1:])

    # (c) Last-resort scan: take the first ALL-CAPS bigram that survives the blocklist.
    if not (first or last):
        for ln in lines:
            tokens = ln.split()
            for i in range(len(tokens) - 1):
                a, b = tokens[i].strip(",.:;"), tokens[i + 1].strip(",.:;")
                if (
                    a.upper() == a and b.upper() == b
                    and _looks_like_name_token(a) and _looks_like_name_token(b)
                ):
                    last, first = a, b
                    break
            if first or last:
                break

    full = _norm_ws(f"{last} {first}").strip()
    return full, first.strip(), last.strip()


def _extract_issuer(text: str) -> str:
    lc = _strip_diacritics(text).lower()
    for needle, canonical in ISSUER_KEYWORDS.items():
        if needle in lc:
            return canonical
    return ""


# ---------------------------------------------------------------------------- #
# Public entrypoint
# ---------------------------------------------------------------------------- #

def extract_insurance_data(pdf_bytes: bytes) -> dict:
    text, lines = _read_pdf_blocks(pdf_bytes)
    if not text:
        return {}

    # VIN — global scan first (VIN regex is highly specific)
    vin_match = VIN_PAT.search(text)
    vin = vin_match.group(1) if vin_match else ""

    # Plate
    plate = ""
    pm = PLATE_PAT.search(text)
    if pm:
        plate = _normalize_plate(pm.group(1))

    # Dates (start, end) — careful not to grab issuance date
    start, end = _extract_dates(text, lines)

    # Name
    full_name, first_name, last_name = _extract_name(lines)

    issuer = _extract_issuer(text)

    return {
        "name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "vin_number": vin.strip(),
        "plate_number": plate.strip(),
        "insurance_start": start,
        "insurance_end": end,
        "issuer": issuer,
    }
