"""
Download a public Google Drive CSV and mark the next UNUSED idea as PROCESSING.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import sys
import re
import urllib.parse
from typing import Dict, List, Tuple

try:
    import requests
except Exception:
    print("Missing dependency: requests. Install with: pip install -r requirements.txt")
    raise

COMMON_SERIAL_NAMES = ["Serial No", "S.No", "S. No", "S No", "Serial", "SNo", "S. No."]
RECOMMENDED_COLUMNS = [
    "Status",
    "Created Date",
    "Processing Date",
    "Published Date",
    "LinkedIn Post ID",
    "LinkedIn URL",
    "Post Content",
    "Image File",
    "Error",
    "Attempts",
]


def download_csv(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    drive_match = re.search(r"/file/d/([^/]+)", parsed.path)
    if parsed.netloc.lower() == "drive.google.com" and drive_match:
        file_id = drive_match.group(1)
        url = f"https://drive.google.com/uc?export=download&id={urllib.parse.quote(file_id)}"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    if "<html" in resp.text[:500].lower() and "drive.google.com" in resp.url:
        raise ValueError("Google Drive returned a viewer page instead of CSV data; verify the file is publicly downloadable")
    return resp.text


def detect_header(fieldnames: List[str]) -> Tuple[str, List[str]]:
    for candidate in COMMON_SERIAL_NAMES:
        for existing in fieldnames:
            if existing.strip().lower() == candidate.strip().lower():
                return existing, fieldnames
    if fieldnames:
        return fieldnames[0], fieldnames
    return "Serial No", fieldnames


def canonical_status(s: str) -> str:
    if s is None:
        return ""
    return s.strip().upper()


def _extract_idea(row: Dict[str, str]) -> str:
    for key in ["Idea", "Idea ", "LinkedIn Post Topic", "LinkedIn Post topic", "Topic", "topic", "Title", "Post Topic"]:
        value = (row.get(key) or "").strip()
        if value:
            return value
    return ""


def find_next_unused(rows: List[Dict[str, str]], serial_col: str) -> int:
    candidates: List[Tuple[int, int]] = []
    for i, row in enumerate(rows):
        serial_raw = row.get(serial_col, "").strip()
        idea = _extract_idea(row)
        if not idea:
            continue
        try:
            serial_val = int(serial_raw)
        except Exception:
            serial_val = 10 ** 9
        status = canonical_status(row.get("Status", ""))
        linkedin_id = (row.get("LinkedIn Post ID") or row.get("LinkedIn Post Id") or "").strip()
        if status in {"PUBLISHED", "READY"}:
            continue
        if linkedin_id:
            continue
        if status == "PROCESSING":
            continue
        candidates.append((serial_val, i))
    if not candidates:
        return -1
    candidates.sort()
    return candidates[0][1]


def ensure_columns(fieldnames: List[str]) -> List[str]:
    out = list(fieldnames)
    for column_name in RECOMMENDED_COLUMNS:
        if not any(existing.strip().lower() == column_name.strip().lower() for existing in out):
            out.append(column_name)
    return out


def mark_row_processing(rows: List[Dict[str, str]], index: int, fieldnames: List[str]) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    row = rows[index]
    row["Status"] = "PROCESSING"
    row["Processing Date"] = now
    attempts_raw = row.get("Attempts", "").strip()
    try:
        attempts = int(attempts_raw) if attempts_raw else 0
    except Exception:
        attempts = 0
    row["Attempts"] = str(attempts + 1)


def write_csv(path: str, rows: List[Dict[str, str]], fieldnames: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run(url: str, output: str) -> int:
    text = download_csv(url)
    f = io.StringIO(text)
    reader = csv.DictReader(f)
    original_fieldnames = reader.fieldnames or []
    rows = list(reader)
    serial_col, _ = detect_header(original_fieldnames)
    fieldnames = ensure_columns(original_fieldnames)
    idx = find_next_unused(rows, serial_col)
    if idx == -1:
        print("No unused ideas found.")
        return 2
    selected = rows[idx]
    idea_text = _extract_idea(selected)
    serial_val = selected.get(serial_col, "").strip()
    mark_row_processing(rows, idx, fieldnames)
    write_csv(output, rows, fieldnames)
    print(f"Marked Serial {serial_val or '(unknown)'} as PROCESSING. Idea: {idea_text}")
    print(f"Updated CSV written to: {output}")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Public Google Drive download URL")
    parser.add_argument("--output", default="Ideas_marked.csv", help="Local output CSV filename")
    args = parser.parse_args(argv)
    try:
        return run(args.url, args.output)
    except Exception as e:
        print("Error:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
