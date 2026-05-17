"""Nelly — the Claude agent that turns chat messages into DB actions.

The agent exposes MCP tools backed by db.py / sheets.py / bofa.py:
  - add_expense:                  one-off insert from a chat message
  - list_expenses:                rows in a date range for summaries / edits
  - update_expense:               PATCH a row by id
  - sync_sheet:                   mirror Supabase -> Google Sheet
  - parse_bofa_csv:               clean a Bank of America transactions CSV
  - import_classified_expenses:   bulk-insert with reference-based dedup
  - check_duplicates:             on-demand fuzzy dedup (same amount, dates ±1d)
  - delete_expense:               permanently delete a row by id
  - save_stock_snapshot:          record pantry inventory for a date (from a photo)
  - list_stock:                   the latest pantry snapshot (current stock)
  - save_menu:                    save/revise weekly menu entries
  - get_menu:                     planned menu for a date range

Short-term memory: messages within a ~45 min window share one Claude Agent
SDK session (resumed by id); after that idle gap a fresh session starts.
"""

import json
import os
import uuid
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

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
    "check_duplicates",
    "Scan the expenses table for potential duplicate pairs (same amount, "
    "dates within 1 day). Use when the user asks to find/check/review "
    "duplicates. Returns pairs to surface for user review — does NOT delete.",
    {},
)
async def check_duplicates(args):
    pairs = db.find_potential_duplicates(window_days=1)
    if not pairs:
        return {"content": [{"type": "text", "text": "No potential duplicates."}]}
    return {"content": [{"type": "text",
        "text": f"Found {len(pairs)} potential duplicate pair(s):\n"
                f"{json.dumps(pairs, indent=2)}"}]}


@tool(
    "delete_expense",
    "Permanently delete an expense by id. Use only when the user explicitly "
    "confirms which row to remove — typically after check_duplicates "
    "surfaced a pair and the user picked one to drop. Irreversible.",
    {"id": str},
)
async def delete_expense(args):
    row = db.delete_expense(args["id"])
    tag_suffix = f" #{row['tag']}" if row.get("tag") else ""
    return {"content": [{"type": "text",
        "text": f"Deleted: {row['date']} {row['description']} "
                f"{row['amount']} {row['currency']} ({row['category']}){tag_suffix}"}]}


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


@tool(
    "save_stock_snapshot",
    "Save the pantry/fridge inventory for a date. Replaces any existing rows "
    "for that date (idempotent — safe to re-run after re-reading a photo). "
    "Use after viewing a fridge photo and enumerating what's inside.",
    {
        "captured_on": str,   # YYYY-MM-DD
        "items_json": str,    # JSON array of {item, quantity, category}
    },
)
async def save_stock_snapshot(args):
    items = json.loads(args["items_json"])
    result = db.replace_stock_snapshot(args["captured_on"], items)
    return {"content": [{"type": "text",
        "text": f"Saved {result['count']} pantry item(s) for {result['date']}."}]}


@tool(
    "list_stock",
    "Return the most recent pantry inventory snapshot (the current stock at "
    "home). Use before building a grocery list or when the user asks what "
    "we have.",
    {},
)
async def list_stock(args):
    rows = db.fetch_latest_stock()
    if not rows:
        return {"content": [{"type": "text",
            "text": "No pantry snapshot yet — send a fridge photo first."}]}
    return {"content": [{"type": "text",
        "text": f"Latest snapshot ({rows[0]['captured_on']}):\n"
                f"{json.dumps(rows, indent=2, ensure_ascii=False)}"}]}


@tool(
    "save_menu",
    "Save (or revise) weekly menu entries. Provide a JSON array of objects "
    "with keys: menu_date (YYYY-MM-DD), meal (one of 'desayuno', 'almuerzo', "
    "'cena'), dish (Spanish), notes (optional), eater ('adulto' or 'bebé', "
    "default 'adulto'). Entries are keyed by (menu_date, meal, eater) — "
    "re-saving a slot overwrites it, no duplicates. The same date+meal can "
    "hold one 'adulto' and one 'bebé' dish.",
    {"menu_json": str},
)
async def save_menu(args):
    rows = json.loads(args["menu_json"])
    result = db.upsert_menu(rows)
    return {"content": [{"type": "text",
        "text": f"Saved {result['count']} menu entr(y/ies)."}]}


@tool(
    "get_menu",
    "Return the planned menu for a date range (inclusive). Use to recall a "
    "week's menu or to build the grocery list.",
    {"start_date": str, "end_date": str},  # both YYYY-MM-DD
)
async def get_menu(args):
    rows = db.fetch_menu(args["start_date"], args["end_date"])
    return {"content": [{"type": "text",
        "text": json.dumps(rows, indent=2, ensure_ascii=False)}]}


_server = create_sdk_mcp_server(
    name="nelly-db", version="1.0.0",
    tools=[
        add_expense, list_expenses, update_expense, sync_sheet,
        parse_bofa_csv, import_classified_expenses,
        check_duplicates, delete_expense,
        save_stock_snapshot, list_stock, save_menu, get_menu,
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
        "When the user asks to check, find, or review duplicates, call "
        "check_duplicates. Present each returned pair as a numbered item "
        "showing both rows side-by-side with their dates, descriptions, "
        "amounts, categories, and ids. Ask the user which one to delete "
        "(or whether to leave both). When they confirm, call delete_expense "
        "with the chosen id. NEVER call delete_expense without an explicit "
        "user instruction — deletion is irreversible. "
        "When the user uploads a Bank of America CSV (you'll see a header "
        "line 'Posted Date,Reference Number,Payee,Address,Amount'), call "
        "parse_bofa_csv with the full CSV text. For each returned row, pick "
        "a leaf category from the vocabulary above and write a short clean "
        "description (strip store numbers, city/state suffixes, and bank "
        "prefixes like TST*, SQ*, PY*). Then call import_classified_expenses "
        "ONCE with the JSON array of all classified rows. Preserve the "
        "amount sign — negative means a refund. Report the inserted and "
        "duplicate counts.\n\n"
        "--- Groceries & meal planning ---\n"
        "Store pantry items and dishes in SPANISH (e.g. 'huevos', 'leche', "
        "'pasta con tomate'), regardless of the language the user writes in. "
        "Meal keys are exactly: 'desayuno', 'almuerzo', 'cena'. Pantry "
        "categories (pick one, Spanish): Verduras, Frutas, Lácteos, Carnes, "
        "Pescados, Granos y pastas, Panadería, Congelados, Bebidas, "
        "Condimentos, Hogar (artículos de hogar no comestibles: papel "
        "higiénico, toallas de papel, servilletas, limpieza), Otros.\n"
        "FRIDGE PHOTO: when the message says a fridge/pantry photo was saved "
        "at a path, use the Read tool on that exact path to view the image, "
        "enumerate every visible food item with a rough quantity in Spanish "
        "(e.g. '6', '1 cartón', 'medio paquete', 'poco'), assign a pantry "
        "category, then call save_stock_snapshot with captured_on = today "
        "(or the date the user states) and the items JSON. Briefly list back "
        "what you recorded.\n"
        "MENU: there is a baby in the household (born around September 2025 — "
        "about 8 months old as of May 2026; estimate the current age from "
        "today's date). When you plan a weekly menu, plan TWO tracks per day: "
        "the adult menu (eater='adulto') and a separate age-appropriate baby "
        "menu (eater='bebé'). For the baby, follow safe infant feeding for "
        "their current age — soft/mashed or finely chopped textures, no added "
        "salt or sugar, no honey, no whole nuts, no choking-hazard shapes, "
        "and prefer simple single/few-ingredient dishes derived from the same "
        "fresh stock where sensible. Propose desayuno, almuerzo and cena for "
        "each day for both eaters, taking current stock into account if "
        "available (call list_stock). After the user confirms (or if they ask "
        "you to just save it), call save_menu with one entry per "
        "(menu_date, meal, eater). To recall a week's menu, call get_menu for "
        "that date range and show it as a day-by-day table split into "
        "Adulto and Bebé.\n"
        "GROCERY LIST: when the user asks what to buy / for the shopping "
        "list, call list_stock AND get_menu for the target week, then reply "
        "with the ingredients BOTH the adult and baby menus need that are NOT "
        "already in stock (or are low), grouped by pantry category. Do NOT "
        "save the grocery list anywhere — it's chat-only.\n\n"
        "Keep replies short, friendly, and in the same language the user wrote."
    )


# --- Short-term conversation memory ----------------------------------------
# Messages within _SESSION_IDLE share one SDK session (resumed by id); after a
# longer silence a fresh session starts. The SDK persists the full transcript
# itself; we only persist the pointer (id + last activity) so it survives a
# systemctl restart. Single user + sequential polling → no locking needed.

_SESSION_IDLE = timedelta(minutes=45)
_SESSION_FILE = Path.home() / ".nelly" / "session.json"


def _load_session() -> tuple[str | None, datetime | None]:
    try:
        data = json.loads(_SESSION_FILE.read_text())
        when = datetime.fromisoformat(data["last_active_at"])
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return data["session_id"], when
    except (OSError, ValueError, KeyError):
        return None, None


def _save_session(session_id: str, when: datetime) -> None:
    _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SESSION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"session_id": session_id, "last_active_at": when.isoformat()}))
    tmp.replace(_SESSION_FILE)


def _pick_session() -> tuple[str, bool]:
    """Returns (session_id, is_fresh). Fresh when there's no prior session or
    the idle gap since last activity has been exceeded."""
    sid, last = _load_session()
    now = datetime.now(timezone.utc)
    if sid is None or last is None or (now - last) > _SESSION_IDLE:
        return str(uuid.uuid4()), True
    return sid, False


def _options(fresh: bool, sid: str) -> ClaudeAgentOptions:
    # session_id pins a new conversation; resume continues an existing one.
    # The SDK forbids combining them (without fork_session), so pass one.
    cont = {"session_id": sid} if fresh else {"resume": sid}
    return ClaudeAgentOptions(
        system_prompt=_system_prompt(),
        mcp_servers={"db": _server},
        allowed_tools=[
            "mcp__db__add_expense",
            "mcp__db__list_expenses",
            "mcp__db__update_expense",
            "mcp__db__sync_sheet",
            "mcp__db__parse_bofa_csv",
            "mcp__db__import_classified_expenses",
            "mcp__db__check_duplicates",
            "mcp__db__delete_expense",
            "mcp__db__save_stock_snapshot",
            "mcp__db__list_stock",
            "mcp__db__save_menu",
            "mcp__db__get_menu",
            "Read",
        ],
        permission_mode="bypassPermissions",
        model="claude-sonnet-4-6",
        cwd=os.path.dirname(os.path.abspath(__file__)),
        **cont,
    )


async def _run_turn(options: ClaudeAgentOptions, text: str) -> str:
    reply_parts: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(text)
        async for msg in client.receive_response():
            # Final assistant text comes back as AssistantMessage blocks
            for block in getattr(msg, "content", []) or []:
                if getattr(block, "text", None):
                    reply_parts.append(block.text)
    return "\n".join(reply_parts).strip() or "(no reply)"


async def handle_message(text: str, image_path: str | None = None) -> str:
    """Run one agent turn. Turns within ~45 min share an SDK session so
    follow-ups have context; after that idle gap a fresh session starts.
    If image_path is given (e.g. a fridge photo), the agent is told to open
    it with the Read tool."""
    if image_path:
        text = (
            f"{text}\n\n[A fridge/pantry photo is saved at {image_path} — "
            f"use the Read tool on that exact path to view it.]"
        )

    sid, fresh = _pick_session()
    try:
        reply = await _run_turn(_options(fresh, sid), text)
    except Exception:
        # A resumed turn failed (e.g. the transcript is gone after an EC2
        # rebuild). Deliberately do NOT auto-retry this turn: tools may have
        # already run and re-running could double-write. Drop the pointer so
        # the NEXT message starts a clean session, and surface the error.
        if not fresh:
            _SESSION_FILE.unlink(missing_ok=True)
        raise

    _save_session(sid, datetime.now(timezone.utc))
    return reply
