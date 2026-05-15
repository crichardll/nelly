"""Supabase REST helpers — insert and fetch expenses.

We use the PostgREST endpoint (https://supabase.com/docs/guides/api) with the
service role key so the bot can write without RLS getting in the way.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

_HEADERS = {
    "apikey": SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
}


def insert_expense(date: str, description: str, amount: float,
                   category: str | None = None, currency: str = "USD",
                   tag: str | None = None) -> dict:
    row = {
        "date": date,
        "description": description,
        "amount": amount,
        "category": category,
        "currency": currency,
    }
    if tag:                       # only include if non-empty so NULL is the default
        row["tag"] = tag
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/expenses",
        json=row,
        headers={**_HEADERS, "Prefer": "return=representation"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()[0]


def fetch_expenses(start_date: str | None = None,
                   end_date: str | None = None) -> list[dict]:
    params = {"select": "id,date,description,category,amount,currency,tag",
              "order": "date.desc"}
    if start_date:
        params["date"] = f"gte.{start_date}"
    if end_date:
        # PostgREST allows multiple filters on the same column via "and"
        params["and"] = f"(date.gte.{start_date or '1900-01-01'},date.lte.{end_date})"
        params.pop("date", None)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/expenses",
        params=params,
        headers=_HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def insert_expenses_bulk(rows: list[dict]) -> dict:
    """POST many rows at once. Duplicates by `bank_reference` are skipped via
    the partial unique index — PostgREST returns only the inserted rows when
    `resolution=ignore-duplicates`. Returns {'inserted': N, 'duplicates': M}."""
    if not rows:
        return {"inserted": 0, "duplicates": 0}
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/expenses",
        params={"on_conflict": "bank_reference"},
        json=rows,
        headers={
            **_HEADERS,
            "Prefer": "return=representation,resolution=ignore-duplicates",
        },
        timeout=30,
    )
    r.raise_for_status()
    inserted = len(r.json())
    return {"inserted": inserted, "duplicates": len(rows) - inserted}


def fetch_all_expenses() -> list[dict]:
    """All rows, all columns, ordered newest-first. Used by the sheet sync."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/expenses",
        params={"select": "*", "order": "date.desc,created_at.desc"},
        headers=_HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def update_expense(id: str, updates: dict) -> dict:
    """PATCH a single expense by id. `updates` is a partial row — only the
    fields present are changed. Returns the updated row."""
    if not updates:
        raise ValueError("update_expense called with no fields to update")
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/expenses",
        params={"id": f"eq.{id}"},
        json=updates,
        headers={**_HEADERS, "Prefer": "return=representation"},
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise ValueError(f"no expense found with id={id}")
    return rows[0]
