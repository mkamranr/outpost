from prices import total


def test_total():
    assert total([{"unit_price": 2}, {"unit_price": 1}]) == 3
