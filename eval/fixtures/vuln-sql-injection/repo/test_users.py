from users import connect, find_by_name


def test_finds_user():
    assert find_by_name(connect(), "alice") == [("alice", "a@example.com")]


def test_quote_in_name_is_data_not_sql():
    # Classic injection: must match nobody instead of every row.
    assert find_by_name(connect(), "x' OR '1'='1") == []
