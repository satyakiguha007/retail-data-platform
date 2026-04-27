"""Static reference data: stores, products, staff.

These are seeds — deterministic inputs that let the generator produce
realistic-looking records without hitting a live database.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Stores  (store_id → city)
# ---------------------------------------------------------------------------
STORES: dict[int, str] = {i: city for i, city in enumerate([
    "Mumbai", "Delhi", "Bangalore", "Hyderabad", "Chennai",
    "Kolkata", "Pune", "Ahmedabad", "Jaipur", "Surat",
    "Lucknow", "Kanpur", "Nagpur", "Patna", "Indore",
    "Thane", "Bhopal", "Visakhapatnam", "Pimpri", "Vadodara",
    "Ghaziabad", "Ludhiana", "Agra", "Nashik", "Faridabad",
    "Meerut", "Rajkot", "Varanasi", "Srinagar", "Aurangabad",
    "Dhanbad", "Amritsar", "Allahabad", "Ranchi", "Howrah",
    "Coimbatore", "Vijayawada", "Jodhpur", "Madurai", "Raipur",
    "Kota", "Guwahati", "Chandigarh", "Solapur", "Hugli",
    "Tiruchirappalli", "Bareilly", "Mysore", "Tiruppur", "Gurgaon",
], start=1)}

# ---------------------------------------------------------------------------
# Merchandise hierarchy  (dept, class, subclass, SKU prefix, description)
# ---------------------------------------------------------------------------
PRODUCTS: list[dict] = [
    # dept 10 — Apparel
    {"dept": 10, "class": 1, "subclass": 1, "sku_prefix": "APP-M-", "desc": "Mens Shirt"},
    {"dept": 10, "class": 1, "subclass": 2, "sku_prefix": "APP-T-", "desc": "Mens Trousers"},
    {"dept": 10, "class": 2, "subclass": 1, "sku_prefix": "APP-W-", "desc": "Womens Kurta"},
    {"dept": 10, "class": 2, "subclass": 2, "sku_prefix": "APP-S-", "desc": "Womens Saree"},
    {"dept": 10, "class": 3, "subclass": 1, "sku_prefix": "APP-K-", "desc": "Kids T-Shirt"},
    # dept 20 — Footwear
    {"dept": 20, "class": 1, "subclass": 1, "sku_prefix": "FTW-C-", "desc": "Casual Shoes"},
    {"dept": 20, "class": 1, "subclass": 2, "sku_prefix": "FTW-F-", "desc": "Formal Shoes"},
    {"dept": 20, "class": 2, "subclass": 1, "sku_prefix": "FTW-S-", "desc": "Sandals"},
    # dept 30 — Electronics
    {"dept": 30, "class": 1, "subclass": 1, "sku_prefix": "ELE-P-", "desc": "Phone Accessory"},
    {"dept": 30, "class": 1, "subclass": 2, "sku_prefix": "ELE-C-", "desc": "Charger"},
    {"dept": 30, "class": 2, "subclass": 1, "sku_prefix": "ELE-H-", "desc": "Headphones"},
    # dept 40 — Home & Kitchen
    {"dept": 40, "class": 1, "subclass": 1, "sku_prefix": "HOM-K-", "desc": "Kitchenware"},
    {"dept": 40, "class": 2, "subclass": 1, "sku_prefix": "HOM-D-", "desc": "Decor"},
    # dept 50 — Beauty & Personal Care
    {"dept": 50, "class": 1, "subclass": 1, "sku_prefix": "BPC-S-", "desc": "Skincare"},
    {"dept": 50, "class": 1, "subclass": 2, "sku_prefix": "BPC-H-", "desc": "Haircare"},
]

# Pre-built SKU pool per product line (5 SKUs each)
SKU_POOL: list[dict] = []
for _p in PRODUCTS:
    for _i in range(1, 6):
        SKU_POOL.append({
            **_p,
            "sku": f"{_p['sku_prefix']}{_i:04d}",
            # Typical retail price range by dept
            "base_price": {10: 799, 20: 1299, 30: 999, 40: 499, 50: 349}.get(_p["dept"], 599)
            + (_i - 1) * 200,
        })

# ---------------------------------------------------------------------------
# Tender types
# ---------------------------------------------------------------------------
TENDER_TYPES: list[dict] = [
    {"tender_type_id": 1, "tender_type_group": "CASH",    "weight": 30},
    {"tender_type_id": 2, "tender_type_group": "CARD",    "weight": 50},
    {"tender_type_id": 3, "tender_type_group": "VOUCHER", "weight": 10},
    {"tender_type_id": 4, "tender_type_group": "CARD",    "weight": 10},  # UPI/wallet
]

# ---------------------------------------------------------------------------
# Staff pools
# ---------------------------------------------------------------------------
CASHIER_IDS: list[str] = [f"CASH{i:04d}" for i in range(1001, 1051)]
SALESPERSON_IDS: list[str] = [f"SP{i:04d}" for i in range(1, 51)]

# ---------------------------------------------------------------------------
# Promotion pool (promo_id, discount %)
# ---------------------------------------------------------------------------
PROMOTIONS: list[dict] = [
    {"promotion": 500001, "disc_pct": 0.10, "rms_promo_type": "PROMO"},
    {"promotion": 500002, "disc_pct": 0.15, "rms_promo_type": "PROMO"},
    {"promotion": 500003, "disc_pct": 0.05, "rms_promo_type": "PROMO"},
    {"promotion": 500004, "disc_pct": 0.20, "rms_promo_type": "PROMO"},
]

# ---------------------------------------------------------------------------
# Tax authorities (for SA_TRAN_IGTAX — Indian GST split)
# ---------------------------------------------------------------------------
IGTAX_AUTHORITIES: list[dict] = [
    {"tax_authority": "CGST", "igtax_code": "GST9",  "igtax_rate": 0.09},
    {"tax_authority": "SGST", "igtax_code": "GST9",  "igtax_rate": 0.09},
]

# Additive tax for SA_TRAN_TAX (used when tax_mode="TAX")
ADDITIVE_TAX: dict = {"tax_code": "GSTTOT", "rate": 0.18}

# ---------------------------------------------------------------------------
# Hour-of-day transaction weight (rush hour modelling)
# ---------------------------------------------------------------------------
HOUR_WEIGHTS: list[float] = [
    0.0,  # 00
    0.0,  # 01
    0.0,  # 02
    0.0,  # 03
    0.0,  # 04
    0.0,  # 05
    0.2,  # 06 — store open
    0.5,  # 07
    1.0,  # 08
    1.5,  # 09
    2.0,  # 10
    2.5,  # 11
    3.0,  # 12 — lunch peak
    2.5,  # 13
    2.0,  # 14
    2.0,  # 15
    2.5,  # 16
    3.0,  # 17 — evening peak
    3.5,  # 18
    3.0,  # 19
    2.0,  # 20
    1.5,  # 21
    1.0,  # 22
    0.5,  # 23
]
