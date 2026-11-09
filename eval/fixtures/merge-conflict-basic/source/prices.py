def total(items):
    """Sum of item prices times their quantity."""
    return sum(item["price"] * item.get("qty", 1) for item in items)
