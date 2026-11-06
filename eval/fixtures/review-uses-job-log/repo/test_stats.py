from stats import mean, median


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_median_even():
    assert median([1, 2, 3, 4]) == 2.5
