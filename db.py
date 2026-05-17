"""Supabase REST helpers — insert and fetch expenses.

We use the PostgREST endpoint (https://supabase.com/docs/guides/api) with the
service role key so the bot can write without RLS getting in the way.
"""

import os
from datetime import date as _date

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


def find_potential_duplicates(window_days: int = 1) -> list[dict]:
    """Pairs of expenses with identical amount and dates within window_days.
    Each pair is {'a': row_a, 'b': row_b} with row_a being the older entry.
    Pairs are sorted with the most-recent pair first."""
    rows = fetch_all_expenses()
    by_amount: dict[float, list[dict]] = {}
    for r in rows:
        by_amount.setdefault(float(r["amount"]), []).append(r)
    pairs: list[dict] = []
    for items in by_amount.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda r: r["date"])
        for i in range(len(items)):
            di = _date.fromisoformat(items[i]["date"])
            for j in range(i + 1, len(items)):
                dj = _date.fromisoformat(items[j]["date"])
                if abs((dj - di).days) <= window_days:
                    pairs.append({"a": items[i], "b": items[j]})
    pairs.sort(key=lambda p: p["b"]["date"], reverse=True)
    return pairs


def delete_expense(id: str) -> dict:
    """Permanently delete a row by id. Returns the deleted row."""
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/expenses",
        params={"id": f"eq.{id}"},
        headers={**_HEADERS, "Prefer": "return=representation"},
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise ValueError(f"no expense found with id={id}")
    return rows[0]


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


# --- Pantry stock (despensa_stock) -----------------------------------------

def replace_stock_snapshot(captured_on: str, items: list[dict]) -> dict:
    """Replace the pantry inventory for a date: delete that date's rows, then
    insert the new ones. Idempotent — re-sending a photo the same day just
    overwrites. `items` is a list of {item, quantity, category}. Returns
    {'date': captured_on, 'count': N}."""
    requests.delete(
        f"{SUPABASE_URL}/rest/v1/despensa_stock",
        params={"captured_on": f"eq.{captured_on}"},
        headers=_HEADERS,
        timeout=15,
    ).raise_for_status()
    if not items:
        return {"date": captured_on, "count": 0}
    rows = [
        {
            "captured_on": captured_on,
            "item": it["item"],
            "quantity": it.get("quantity") or None,
            "category": it.get("category") or None,
        }
        for it in items
    ]
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/despensa_stock",
        json=rows,
        headers={**_HEADERS, "Prefer": "return=representation"},
        timeout=30,
    )
    r.raise_for_status()
    return {"date": captured_on, "count": len(r.json())}


def fetch_latest_stock() -> list[dict]:
    """The most recent pantry snapshot (= current stock). Empty list if the
    table has no rows yet."""
    head = requests.get(
        f"{SUPABASE_URL}/rest/v1/despensa_stock",
        params={"select": "captured_on", "order": "captured_on.desc", "limit": 1},
        headers=_HEADERS,
        timeout=15,
    )
    head.raise_for_status()
    latest = head.json()
    if not latest:
        return []
    day = latest[0]["captured_on"]
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/despensa_stock",
        params={"select": "id,captured_on,item,quantity,category",
                "captured_on": f"eq.{day}",
                "order": "category.asc,item.asc"},
        headers=_HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# --- Weekly menu (weekly_menu) ---------------------------------------------

def upsert_menu(rows: list[dict]) -> dict:
    """Insert/replace menu entries keyed by (menu_date, meal, eater). Each row
    is {menu_date, meal, dish, notes, eater}. `eater` is 'adulto' (default) or
    'bebé' — the same slot can hold one of each. Returns {'count': N}."""
    if not rows:
        return {"count": 0}
    payload = [
        {
            "menu_date": r["menu_date"],
            "meal": r["meal"],
            "dish": r["dish"],
            "notes": r.get("notes") or None,
            "eater": r.get("eater") or "adulto",
        }
        for r in rows
    ]
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/weekly_menu",
        params={"on_conflict": "menu_date,meal,eater"},
        json=payload,
        headers={
            **_HEADERS,
            "Prefer": "return=representation,resolution=merge-duplicates",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return {"count": len(resp.json())}


def fetch_menu(start_date: str, end_date: str) -> list[dict]:
    """Menu entries with menu_date in [start_date, end_date], ordered by
    date, eater, then meal."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/weekly_menu",
        params={"select": "id,menu_date,meal,dish,notes,eater",
                "and": f"(menu_date.gte.{start_date},menu_date.lte.{end_date})",
                "order": "menu_date.asc,eater.asc,meal.asc"},
        headers=_HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()
