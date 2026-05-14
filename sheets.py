"""Google Sheets sync — overwrite a tab with all expenses.

One-way mirror: clear the target tab, write [headers] + all rows from Supabase,
done. Idempotent. ~1s for hundreds of rows.

Auth: service account JSON key whose path is in `GOOGLE_CREDENTIALS_FILE`. The
service account email must be shared on the sheet as Editor — `service_account_email()`
returns it so error messages can tell the user whom to share with.
"""

import os
import gspread
from dotenv import load_dotenv

load_dotenv()

# Pinned column order. Don't rely on dict.keys() from PostgREST — pinning here
# protects the sheet against an accidental column reorder in the DB.
COLUMNS = [
    "id", "date", "description", "category", "amount",
    "currency", "paid_by", "notes", "tag", "created_at",
]
_NUMERIC = {"amount"}  # PostgREST returns numeric as string — coerce these


def _client() -> gspread.Client:
    return gspread.service_account(filename=os.environ["GOOGLE_CREDENTIALS_FILE"])


def _row(record: dict) -> list:
    out = []
    for col in COLUMNS:
        v = record.get(col)
        if v is None:
            out.append("")
        elif col in _NUMERIC:
            out.append(float(v))
        else:
            out.append(v)
    return out


def sync_to_sheet(rows: list[dict]) -> int:
    """Overwrite the configured tab with the given rows. Returns row count."""
    gc = _client()
    sh = gc.open_by_key(os.environ["GOOGLE_SHEET_ID"])
    ws = sh.worksheet(os.environ.get("GOOGLE_SHEET_TAB", "BBDD_Gastos"))
    body = [COLUMNS] + [_row(r) for r in rows]
    ws.clear()
    # USER_ENTERED so Sheets parses dates as dates and numbers as numbers,
    # rather than storing everything as text.
    ws.update("A1", body, value_input_option="USER_ENTERED")
    return len(rows)


def service_account_email() -> str:
    """The email to share the sheet with. Surfaced in error messages."""
    return _client().auth.service_account_email
