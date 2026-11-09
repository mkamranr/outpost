import sqlite3


def connect():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (name TEXT, email TEXT)")
    conn.executemany("INSERT INTO users VALUES (?, ?)", [("alice", "a@example.com"), ("bob", "b@example.com")])
    return conn


def find_by_name(conn, name):
    cur = conn.execute(f"SELECT name, email FROM users WHERE name = '{name}'")
    return cur.fetchall()
