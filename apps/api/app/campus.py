"""Fixed campus data shared by the agent, the matcher and the seed script.

Single source of truth in Python for locations, zones, categories and aliases
(PROJECT_GUIDE.md §10 and §22). db/schema.sql inserts the same 14 locations.
"""

from __future__ import annotations

LOCATIONS: dict[str, str] = {
    "CSE Block": "academic",
    "Mech Block": "academic",
    "Civil Block": "academic",
    "E&TC Block": "academic",
    "Workshop": "academic",
    "Central Library": "academic",
    "Auditorium": "common",
    "Main Canteen": "common",
    "Admin Building": "common",
    "Bus Stop": "common",
    "Parking Lot": "common",
    "Sports Ground": "sports",
    "Boys Hostel A": "hostel",
    "Girls Hostel B": "hostel",
}

# Extra ways students refer to each place (all lowercase).
LOCATION_ALIASES: dict[str, list[str]] = {
    "CSE Block": ["cse block", "cse dept", "cse department", "computer department",
                  "computer dept", "comp block", "cs block", "cse"],
    "Mech Block": ["mech block", "mechanical block", "mechanical department", "mech dept",
                   "mechanical", "mech"],
    "Civil Block": ["civil block", "civil department", "civil dept", "civil"],
    "E&TC Block": ["e&tc block", "entc block", "etc block", "extc block", "e&tc", "entc",
                   "extc", "electronics block"],
    "Workshop": ["workshop"],
    "Central Library": ["central library", "library", "reading hall", "lib"],
    "Auditorium": ["auditorium", "audi"],
    "Main Canteen": ["main canteen", "canteen", "cafeteria", "cafe"],
    "Admin Building": ["admin building", "administration building", "admin block",
                       "admin office"],
    "Bus Stop": ["bus stop", "bus stand"],
    "Parking Lot": ["parking lot", "parking area", "parking"],
    "Sports Ground": ["sports ground", "playground", "football ground", "cricket ground",
                      "ground"],
    "Boys Hostel A": ["boys hostel a", "boys hostel", "bh a", "bh-a"],
    "Girls Hostel B": ["girls hostel b", "girls hostel", "gh b", "gh-b"],
}

CATEGORIES: tuple[str, ...] = (
    "bag", "bottle", "electronics", "id_card", "wallet", "keys",
    "book", "clothing", "jewellery", "stationery", "other",
)

HOLDING_PLACES: tuple[str, ...] = ("finder", "admin_office", "security")

HOLDING_LABELS: dict[str, str] = {
    "finder": "with the finder",
    "admin_office": "at the admin office",
    "security": "with campus security",
}


def zone_of(location: str | None) -> str | None:
    """Zone for a canonical location name, or None."""
    return LOCATIONS.get(location) if location else None
