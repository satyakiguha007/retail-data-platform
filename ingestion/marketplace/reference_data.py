"""Static reference data for the marketplace feed simulator."""

from __future__ import annotations

# Marketplace definitions
# store_countries: which country codes this marketplace services
# currency: local transaction currency
# commission_rate: fraction of order total charged to the seller
MARKETPLACES: list[dict] = [
    {"marketplace": "AMAZON_IN",  "store_countries": ["India"],     "currency": "INR", "commission_rate": 0.15},
    {"marketplace": "FLIPKART_IN","store_countries": ["India"],     "currency": "INR", "commission_rate": 0.12},
    {"marketplace": "MYNTRA_IN",  "store_countries": ["India"],     "currency": "INR", "commission_rate": 0.18},
    {"marketplace": "AMAZON_US",  "store_countries": ["USA"],       "currency": "USD", "commission_rate": 0.15},
    {"marketplace": "AMAZON_UK",  "store_countries": ["UK"],        "currency": "GBP", "commission_rate": 0.15},
    {"marketplace": "AMAZON_AE",  "store_countries": ["UAE"],       "currency": "AED", "commission_rate": 0.15},
    {"marketplace": "LAZADA_SG",  "store_countries": ["Singapore"], "currency": "SGD", "commission_rate": 0.10},
]

# Online SKU pool — wider assortment than POS, higher average selling price
# Fields match pos_simulator SKU_POOL for Silver conformance compatibility
SKU_POOL: list[dict] = [
    # Electronics — high ASP
    {"sku": "ELEC-001", "dept": "D10", "class": "C101", "subclass": "S1001", "base_price": 25000, "category": "Electronics"},
    {"sku": "ELEC-002", "dept": "D10", "class": "C101", "subclass": "S1001", "base_price": 55000, "category": "Electronics"},
    {"sku": "ELEC-003", "dept": "D10", "class": "C102", "subclass": "S1002", "base_price": 12000, "category": "Electronics"},
    {"sku": "ELEC-004", "dept": "D10", "class": "C102", "subclass": "S1002", "base_price": 3500,  "category": "Electronics"},
    {"sku": "ELEC-005", "dept": "D10", "class": "C103", "subclass": "S1003", "base_price": 8500,  "category": "Electronics"},
    # Fashion — medium ASP
    {"sku": "FASH-001", "dept": "D20", "class": "C201", "subclass": "S2001", "base_price": 1800,  "category": "Fashion"},
    {"sku": "FASH-002", "dept": "D20", "class": "C201", "subclass": "S2001", "base_price": 3200,  "category": "Fashion"},
    {"sku": "FASH-003", "dept": "D20", "class": "C202", "subclass": "S2002", "base_price": 4500,  "category": "Fashion"},
    {"sku": "FASH-004", "dept": "D20", "class": "C202", "subclass": "S2002", "base_price": 2200,  "category": "Fashion"},
    {"sku": "FASH-005", "dept": "D20", "class": "C203", "subclass": "S2003", "base_price": 6500,  "category": "Fashion"},
    # Home & Kitchen — varied ASP
    {"sku": "HOME-001", "dept": "D30", "class": "C301", "subclass": "S3001", "base_price": 9999,  "category": "Home"},
    {"sku": "HOME-002", "dept": "D30", "class": "C301", "subclass": "S3001", "base_price": 2499,  "category": "Home"},
    {"sku": "HOME-003", "dept": "D30", "class": "C302", "subclass": "S3002", "base_price": 18500, "category": "Home"},
    {"sku": "HOME-004", "dept": "D30", "class": "C302", "subclass": "S3002", "base_price": 750,   "category": "Home"},
    # Books & Media — low ASP
    {"sku": "BOOK-001", "dept": "D40", "class": "C401", "subclass": "S4001", "base_price": 499,   "category": "Books"},
    {"sku": "BOOK-002", "dept": "D40", "class": "C401", "subclass": "S4001", "base_price": 799,   "category": "Books"},
    # Sports & Outdoors
    {"sku": "SPRT-001", "dept": "D50", "class": "C501", "subclass": "S5001", "base_price": 5999,  "category": "Sports"},
    {"sku": "SPRT-002", "dept": "D50", "class": "C501", "subclass": "S5001", "base_price": 1299,  "category": "Sports"},
    # Beauty & Personal Care
    {"sku": "BEAU-001", "dept": "D60", "class": "C601", "subclass": "S6001", "base_price": 899,   "category": "Beauty"},
    {"sku": "BEAU-002", "dept": "D60", "class": "C601", "subclass": "S6001", "base_price": 1499,  "category": "Beauty"},
]

# Order status distribution for settled feed
# DELIVERED: paid & fulfilled; RETURNED: customer return + refund already processed
ORDER_STATUS_WEIGHTS: dict[str, int] = {
    "DELIVERED":       88,
    "RETURNED":        10,
    "CANCELLED_REFUND": 2,
}

# Promotion pool — online discounts tend to be deeper than POS
PROMOTIONS: list[dict] = [
    {"promo_code": "SALE10",  "disc_pct": 0.10},
    {"promo_code": "SAVE15",  "disc_pct": 0.15},
    {"promo_code": "FLASH20", "disc_pct": 0.20},
    {"promo_code": "DEAL25",  "disc_pct": 0.25},
    {"promo_code": "BIGSALE", "disc_pct": 0.30},
]

# Settlement lag in days (order_date + lag = settle_date)
# Distributed: most orders settle in 2-4 days
SETTLEMENT_LAG_WEIGHTS: dict[int, int] = {2: 30, 3: 40, 4: 20, 5: 7, 6: 3}
