def total(items):
    """Sum of item prices (prices are per unit)."""
    return sum(item["unit_price"] for item in items)
