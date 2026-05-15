"""Expense category vocabulary.

Two-level hierarchy: parent groups (with emoji for readability) → leaf
categories. Only the leaf string is stored in the DB. The parent group lives
only in the system prompt as a mental scaffold for Claude to pick the right
leaf — if you want per-group rollups later, map child→parent at read time
using this same dict.
"""

CATEGORIES: dict[str, list[str]] = {
    "🏠 Home": [
        "Furnishings", "Electronics", "Rent / Mortgage", "Repairs",
        "Cleaning", "Other Home",
    ],
    "🍔 Food & Drink": [
        "Dining out", "Groceries", "Liquor store", "Coffee",
        "Other Food & Drink",
    ],
    "🚗 Transportation": [
        "Car Rental", "Gas / Fuel", "Parking", "Public Transportation",
        "Taxi / Uber", "Service & Parts", "Other Transportation",
    ],
    "🎭 Entertainment": [
        "Movies / Films", "Sports events", "Games", "Books", "Music",
        "Other Entertainment",
    ],
    "💡 Utilities": [
        "Electricity", "Gas", "Water", "Trash", "Internet",
        "Cable / Satellite", "Phone", "Other Utilities",
    ],
    "🧘 Life": [
        "Medical", "Pets", "Clothing", "Insurance",
        "Education / Student Loans", "Childcare", "Personal Care",
        "Sports / Fitness", "Spa / Massage", "Life - General",
    ],
    "🌴 Vacation": [
        "Flights", "Hotels / Lodging", "Vacation Car Rental",
        "Tours / Activities", "Vacation Dining", "Other Vacation",
    ],
    "🎁 General": ["Uncategorized", "Other"],
}


def format_for_prompt() -> str:
    """Render the hierarchy as a bullet list for the system prompt."""
    return "\n".join(
        f"- {parent}: " + ", ".join(children)
        for parent, children in CATEGORIES.items()
    )


def all_leaves() -> list[str]:
    """Flat list of every leaf category — useful for validation if we add it later."""
    return [leaf for leaves in CATEGORIES.values() for leaf in leaves]
