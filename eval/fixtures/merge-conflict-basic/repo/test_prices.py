from prices import total


def test_total():
    assert total([{"price": 2}, {"price": 1}]) == 3
