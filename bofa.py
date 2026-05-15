"""Parse a Bank of America credit-card transactions CSV.

The agent uses this via the parse_bofa_csv tool. It does only filtering and
normalization — never touches Supabase. Classification (description cleanup
and category picking) happens in the agent loop afterwards.

Filters out rows we don't want to import:
  - pending transactions (empty Posted Date or reference starts with TEMP)
  - credit-card payments from a linked checking account ("PAYMENT FROM ...")

Sign convention in the returned dict:
  - charges (BofA encodes as negative) → positive amount (an expense)
  - refunds (BofA encodes as positive) → negative amount (reduces net spend)
"""

import csv
import io
from datetime import datetime


def parse(csv_text: str) -> list[dict]:
    out: list[dict] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        posted = (row.get("Posted Date") or "").strip()
        ref = (row.get("Reference Number") or "").strip()
        payee = (row.get("Payee") or "").strip()
        amount_raw = (row.get("Amount") or "").strip()

        # Skip pending and malformed rows.
        if not posted or not ref or ref.startswith("TEMP") or not amount_raw:
            continue
        try:
            amount = float(amount_raw)
        except ValueError:
            continue

        # Internal CC payments — skip. Other positives are refunds — keep, with
        # sign flipped so they store as negative-amount expenses.
        if amount > 0 and "PAYMENT FROM" in payee.upper():
            continue

        # BofA: charges arrive as negatives; we want amount > 0 for an expense.
        amount = -amount

        out.append({
            "bank_reference": ref,
            "date": datetime.strptime(posted, "%m/%d/%Y").date().isoformat(),
            "payee": payee,
            "amount": round(amount, 2),
        })
    return out
