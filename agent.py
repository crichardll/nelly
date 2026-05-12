"""Nelly — the Claude agent that turns chat messages into DB actions.

The agent exposes two MCP tools backed by db.py:
  - add_expense:   parses "lunch 25" style messages and writes a row
  - list_expenses: pulls rows for a date range so Claude can summarize them

Each Telegram message is one independent agent turn (no chat memory).
"""

import os
from datetime import date as _date

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)

import db


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


_server = create_sdk_mcp_server(
    name="nelly-db", version="1.0.0",
    tools=[add_expense, list_expenses, update_expense],
)


def _system_prompt() -> str:
    return (
        f"You are Nelly, a personal expense-tracking assistant. "
        f"Today is {_date.today().isoformat()}. "
        "When the user describes a purchase, infer the amount, a short "
        "description, a category (Dining out, Taxi, Groceries, Liquor, "
        "Entertainment, Travel, Other), and call add_expense. If no date "
        "is given, use today. Default currency USD. "
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
