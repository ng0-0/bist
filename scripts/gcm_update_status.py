import json
import os
import re
from urllib.request import urlopen
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_JSON_PATH = ROOT_DIR / "data.json"
STATUS_JSON_PATH = ROOT_DIR / "gcm_update_status.json"
GCM_URL = "https://www.gcmyatirim.com.tr/arastirma-analiz/yurt-ici-bilanco-takvimi"
DEFAULT_QUARTER = os.environ.get("GCM_QUARTER_LABEL", "2026 1. Çeyrek")
DEFAULT_DATA_JSON_URL = os.environ.get(
    "DATA_JSON_URL",
    "https://dl.dropboxusercontent.com/scl/fi/eiyktxtdnm3jp32hvqnev/data.json?rlkey=my50wvz5dkox9ss3v7j9cvn7e",
)

ROW_RE = re.compile(
    r"([A-ZÇĞİÖŞÜa-zçğıöşü0-9 .,&'’/\\-*]+?\(([A-Z0-9]{4,5})\))"
    r"\s+(\d{2}-\d{2}-\d{4})"
    r"(?:\s+([0-9.,-]+|Bekleniyor|bekleniyor|BEKLENIYOR|0))?"
    r"(?:\s+([0-9.,-]+|Bekleniyor|bekleniyor|BEKLENIYOR|0))?"
)


def parse_date_safe(value):
    if not value:
        return None

    text = str(value).strip()
    today = datetime.now().date()

    lowered = text.lower()
    if lowered in {"bugün", "bugun"}:
        return today
    if lowered in {"dün", "dun"}:
        from datetime import timedelta
        return today - timedelta(days=1)

    for fmt in ("%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    return None


def normalize_date(value):
    parsed = parse_date_safe(value)
    if not parsed:
        return None
    return parsed.strftime("%d.%m.%Y")


def normalize_gcm_date(value):
    try:
        return datetime.strptime(str(value).strip(), "%d-%m-%Y").strftime("%d.%m.%Y")
    except ValueError:
        return str(value).strip()


def load_local_bilanco_dates():
    payload = None

    if DATA_JSON_PATH.exists():
        payload = json.loads(DATA_JSON_PATH.read_text(encoding="utf-8"))
    else:
        with urlopen(DEFAULT_DATA_JSON_URL, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))

    mapping = {}
    for row in payload:
        code = str(row.get("Kod") or "").strip().upper()
        if not code:
            continue
        mapping[code] = normalize_date(row.get("Bilanco_Tarih"))
    return mapping


def extract_rows(raw_text):
    text = re.sub(r"\s+", " ", raw_text).strip()
    rows = []
    seen = set()

    for match in ROW_RE.finditer(text):
        company = match.group(1).strip()
        code = match.group(2).strip().upper()
        date_raw = match.group(3).strip()
        expected = (match.group(4) or "").strip()
        declared = (match.group(5) or "").strip()

        key = (code, date_raw, expected, declared)
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            {
                "company": company,
                "code": code,
                "gcm_date": normalize_gcm_date(date_raw),
                "expected": expected,
                "declared": declared,
            }
        )

    return rows


def try_select_quarter(page, quarter_label):
    candidates = [
        page.get_by_text(quarter_label, exact=True).first,
        page.locator("text=" + quarter_label).first,
    ]
    for candidate in candidates:
        try:
            candidate.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


def scrape_rows(quarter_label):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(GCM_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        try_select_quarter(page, quarter_label)
        page.wait_for_timeout(2000)

        try:
            page.wait_for_selector(f"text={quarter_label}", timeout=5000)
        except PlaywrightTimeoutError:
            print(f"Warning: '{quarter_label}' dogrulanamadi, mevcut gorunum okunuyor.")

        body_text = page.locator("body").inner_text(timeout=30000)
        browser.close()

    rows = extract_rows(body_text)
    if not rows:
        raise RuntimeError("GCM sayfasindan satir parse edilemedi.")
    return rows


def is_announced(row):
    declared = str(row.get("declared") or "").strip()
    if not declared:
        return False
    return "beklen" not in declared.lower()


def compare_rows(rows, local_dates):
    today = datetime.now().date()
    updates = []
    same = []
    waiting = []

    for row in rows:
        code = row["code"]
        gcm_date = row["gcm_date"]
        gcm_date_obj = parse_date_safe(gcm_date)
        local_date = local_dates.get(code)
        announced = is_announced(row)
        is_past_or_today = gcm_date_obj is not None and gcm_date_obj <= today

        result = {**row, "json_date": local_date}

        if announced and is_past_or_today:
            if local_date != gcm_date:
                result["status"] = "UPDATE"
                updates.append(result)
            else:
                result["status"] = "SAME"
                same.append(result)
        else:
            result["status"] = "WAITING"
            waiting.append(result)

    updates.sort(key=lambda x: parse_date_safe(x["gcm_date"]) or datetime.min.date(), reverse=True)
    return updates, same, waiting


def write_status_file(quarter_label, updates, same, waiting):
    payload = {
        "updated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "quarter": quarter_label,
        "count": len(updates),
        "symbols": [row["code"] for row in updates],
        "same_count": len(same),
        "waiting_count": len(waiting),
    }
    STATUS_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main():
    quarter_label = DEFAULT_QUARTER
    local_dates = load_local_bilanco_dates()
    rows = scrape_rows(quarter_label)
    updates, same, waiting = compare_rows(rows, local_dates)
    write_status_file(quarter_label, updates, same, waiting)
    print(f"GCM update count: {len(updates)}")
    if updates:
        print("Symbols:", ", ".join(row["code"] for row in updates))


if __name__ == "__main__":
    main()
