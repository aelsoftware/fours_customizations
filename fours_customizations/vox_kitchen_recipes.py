"""Owner-approved, per-plate Vox Nansana recipes; no reads or side effects.

These exact item identities are dedicated to this kitchen. Optional menu
modifiers remain priced instructions: only the fixed bowl recipe includes
Irish potatoes. No unspecified modifier ingredient portions are inferred.
"""

from decimal import Decimal


FINISHED_UOM = "Pc(s)"
LEGACY_MENU = frozenset(("VXN-DRY-GOAT", "VXN-PAN-FRIED-GOAT"))
MENU = {
    "VXN-DRY-GOAT": "BOM-VXN-DRY-GOAT-001",
    "VXN-PAN-FRIED-GOAT": "BOM-VXN-PAN-FRIED-GOAT-001",
    "VXN-CHICKEN": "BOM-VXN-CHICKEN-001",
    "VXN-CHICKEN-BOWL": "BOM-VXN-CHICKEN-BOWL-001",
    "VXN-GOAT-BOWL": "BOM-VXN-GOAT-BOWL-001",
}
BATCHES = {
    "VXN-DRY-GOAT": Decimal("25"),
    "VXN-PAN-FRIED-GOAT": Decimal("25"),
    "VXN-CHICKEN": Decimal("4"),
    "VXN-CHICKEN-BOWL": Decimal("4"),
    "VXN-GOAT-BOWL": Decimal("25"),
}
RAW_UOMS = {
    "VXN-RAW-GOAT-KG": "Kg",
    "VXN-RAW-CHICKEN-BIRD": "Bird",
    "VXN-RAW-IRISH-KG": "Kg",
}
# Order is canonical snapshot order, not an instruction to reorder native
# BOM/Stock Entry rows. Native physical rows are compared by unique item code.
COMPONENTS = {
    "VXN-DRY-GOAT": (("VXN-RAW-GOAT-KG", "Kg", Decimal("0.4")),),
    "VXN-PAN-FRIED-GOAT": (("VXN-RAW-GOAT-KG", "Kg", Decimal("0.4")),),
    "VXN-CHICKEN": (("VXN-RAW-CHICKEN-BIRD", "Bird", Decimal("0.25")),),
    "VXN-CHICKEN-BOWL": (
        ("VXN-RAW-CHICKEN-BIRD", "Bird", Decimal("0.25")),
        ("VXN-RAW-IRISH-KG", "Kg", Decimal("0.1")),
    ),
    "VXN-GOAT-BOWL": (
        ("VXN-RAW-GOAT-KG", "Kg", Decimal("0.4")),
        ("VXN-RAW-IRISH-KG", "Kg", Decimal("0.1")),
    ),
}
