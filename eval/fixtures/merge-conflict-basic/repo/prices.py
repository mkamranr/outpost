def total(items):
    """Sum of item prices."""
    return sum(item["price"] for item in items)
