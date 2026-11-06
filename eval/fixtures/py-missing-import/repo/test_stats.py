from stats import rms


def test_rms():
    assert rms([3, 4, 0, 0]) == 2.5
