from prices import total


def test_total():
    assert total([{"price": 2}, {"price": 1}]) == 3


def test_quantity():
    assert total([{"price": 2, "qty": 3}, {"price": 1}]) == 7
