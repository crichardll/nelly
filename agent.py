"""Nelly — the Claude agent that turns chat messages into DB actions.

The agent exposes MCP tools backed by db.py / sheets.py / bofa.py:
  - add_expense:                  one-off insert from a chat message
  - list_expenses:                rows in a date range for summaries / edits
  - update_expense:               PATCH a row by id
  - sync_sheet:                   mirror Supabase -> Google Sheet
  - parse_bofa_csv:               clean a Bank of America transactions CSV
  - import_classified_expenses:   bulk-insert with reference-based dedup

Each Telegram message is one independent agent turn (no chat memory).
"""

import json
import os
from datetime import date as _date

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)

import bofa
import categories
import db
import sheets


@tool(
    "add_expense",
    "Record a new expense in the database.",
    {
        "date": str,         # YYYY-MM-DD
        "description": str,
        "amount": float,
        "category": str,     # e.g. "Dining out", "Taxi", "Groceries"
        "currency": str,     # default USD
        "tag": str,          # empty string means "no tag"
    },
)
async def add_expense(args):
    row = db.insert_expense(
        date=args["date"],
        description=args["description"],
        amount=float(args["amount"]),
        category=args.get("category"),
        currency=args.get("currency") or "USD",
        tag=args.get("tag") or None,   # "" → None → NULL in DB
    )
    tag_suffix = f" #{row['tag']}" if row.get("tag") else ""
    return {"content": [{"type": "text", "text":
        f"Saved: {row['date']} {row['description']} "
        f"{row['amount']} {row['currency']} ({row['category']}){tag_suffix}"}]}


@tool(
    "list_expenses",
    "Return all expenses in the given date range (inclusive). "
    "Use this to answer summary/total/breakdown questions, or to look up "
    "a row's id before calling update_expense.",
    {"start_date": str, "end_date": str},  # both YYYY-MM-DD
)
async def list_expenses(args):
    rows = db.fetch_expenses(args["start_date"], args["end_date"])
    return {"content": [{"type": "text", "text": str(rows)}]}


@tool(
    "update_expense",
    "Update fields of an existing expense by id. Pass an empty string "
    "for any field you don't want to change. Always call list_expenses "
    "first to find the row's id.",
    {
        "id": str,            # uuid of the row to update
        "date": str,          # YYYY-MM-DD or ""
        "description": str,
        "amount": str,        # str so "" can mean "no change"; converted below
        "category": str,
        "currency": str,
        "tag": str,
    },
)
async def update_expense(args):
    updates: dict = {}
    if args.get("date"):
        updates["date"] = args["date"]
    if args.get("description"):
        updates["description"] = args["description"]
    if args.get("amount"):
        updates["amount"] = float(args["amount"])
    if args.get("category"):
        updates["category"] = args["category"]
    if args.get("currency"):
        updates["currency"] = args["currency"]
    if args.get("tag"):
        updates["tag"] = args["tag"]

    if not updates:
        return {"content": [{"type": "text",
            "text": "No fields to change — nothing was updated."}]}

    row = db.update_expense(args["id"], updates)
    tag_suffix = f" #{row['tag']}" if row.get("tag") else ""
    return {"content": [{"type": "text", "text":
        f"Updated: {row['date']} {row['description']} "
        f"{row['amount']} {row['currency']} ({row['category']}){tag_suffix}"}]}


@tool(
    "sync_sheet",
    "Overwrite the Google Sheet with all current expenses. "
    "Use when the user asks to sync, export, refresh, or update the spreadsheet.",
    {},
)
async def sync_sheet(args):
    try:
        rows = db.fetch_all_expenses()
        n = sheets.sync_to_sheet(rows)
        return {"content": [{"type": "text",
            "text": f"Synced {n} rows to BBDD_Gastos."}]}
    except Exception as e:
        msg = f"Sheet sync failed: {type(e).__name__}: {e}"
        # The most common first-run failure is forgetting to share the sheet
        # with the service-account email. Surface it explicitly.
        if "PERMISSION_DENIED" in str(e) or "403" in str(e):
            try:
                msg += (f"\nShare the sheet with "
                        f"{sheets.service_account_email()} as Editor.")
            except Exception:
                pass
        return {"content": [{"type": "text", "text": msg}]}


@tool(
    "parse_bofa_csv",
    "Parse a Bank of America transactions CSV. Returns the rows ready to "
    "classify and insert. Pending rows and credit-card payments are already "
    "filtered out. The amount field is positive for charges and negative for "
    "refunds — preserve that sign when you insert.",
    {"csv_text": str},
)
async def parse_bofa_csv(args):
    rows = bofa.parse(args["csv_text"])
    return {"content": [{"type": "text",
        "text": f"{len(rows)} rows after filtering:\n{json.dumps(rows, indent=2)}"}]}


@tool(
    "import_classified_expenses",
    "Bulk-insert pre-classified expenses. Provide a JSON array of objects "
    "with keys: bank_reference, date (YYYY-MM-DD), description (short and "
    "clean — strip store numbers / city codes / bank prefixes like TST*), "
    "category (must be a leaf from the vocabulary), amount (number; "
    "negative for refunds), currency (default USD). Duplicates by "
    "bank_reference are silently skipped — safe to re-run.",
    {"expenses_json": str},
)
async def import_classified_expenses(args):
    rows = json.loads(args["expenses_json"])
    for r in rows:
        r.setdefault("currency", "USD")
    result = db.insert_expenses_bulk(rows)
    return {"content": [{"type": "text",
        "text": f"Imported {result['inserted']} new expense(s); "
                f"skipped {result['duplicates']} duplicate(s)."}]}


_server = create_sdk_mcp_server(
    name="nelly-db", version="1.0.0",
    tools=[
        add_expense, list_expenses, update_expense, sync_sheet,
        parse_bofa_csv, import_classified_expenses,
    ],
)


def _system_prompt() -> str:
    return (
        f"You are Nelly, a personal expense-tracking assistant. "
        f"Today is {_date.today().isoformat()}. "
        "When the user describes a purchase, infer the amount, a short "
        "description, the most specific leaf category from the list below, "
        "and call add_expense. Pick exactly the spelling shown — do not "
        "invent new categories. If genuinely unsure, use 'Uncategorized'. "
        "If no date is given, use today. Default currency USD.\n\n"
        "Categories (pick a leaf, never a parent group):\n"
        f"{categories.format_for_prompt()}\n\n"
        "Include a tag ONLY if the user explicitly mentions one "
        "(e.g. 'tag: work', '#trip-tokyo', 'use tag travel'). "
        "Otherwise pass an empty string for tag. "
        "When the user asks for a summary or total, call list_expenses for "
        "the right date range, then reply with a short markdown breakdown. "
        "When the user wants to change an existing expense, first call "
        "list_expenses to find candidate rows, identify the right one by "
        "description/date/amount, then call update_expense with that row's "
        "id and empty strings for fields that shouldn't change. If more than "
        "one expense plausibly matches, ask the user to clarify before "
        "updating. "
        "If the user asks to sync, export, refresh, or update the spreadsheet, "
        "call sync_sheet and report back with the row count. "
        "When the user uploads a Bank of America CSV (you'll see a header "
        "line 'Posted Date,Reference Number,Payee,Address,Amount'), call "
        "parse_bofa_csv with the full CSV text. For each returned row, pick "
        "a leaf category from the vocabulary above and write a short clean "
        "description (strip store numbers, city/state suffixes, and bank "
        "prefixes like TST*, SQ*, PY*). Then call import_classified_expenses "
        "ONCE with the JSON array of all classified rows. Preserve the "
        "amount sign — negative means a refund. Report the inserted and "
        "duplicate counts. "
        "Keep replies short, friendly, and in the same language the user wrote."
    )


async def handle_message(text: str) -> str:
    """Run one agent turn on a single user message. Returns the final reply."""
    options = ClaudeAgentOptions(
        system_prompt=_system_prompt(),
        mcp_servers={"db": _server},
        allowed_tools=[
            "mcp__db__add_expense",
            "mcp__db__list_expenses",
            "mcp__db__update_expense",
            "mcp__db__sync_sheet",
            "mcp__db__parse_bofa_csv",
            "mcp__db__import_classified_expenses",
        ],
        permission_mode="bypassPermissions",
        model="claude-sonnet-4-6",
    )

    reply_parts: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(text)
        async for msg in client.receive_response():
            # Final assistant text comes back as AssistantMessage blocks
            for block in getattr(msg, "content", []) or []:
                if getattr(block, "text", None):
                    reply_parts.append(block.text)
    return "\n".join(reply_parts).strip() or "(no reply)"
