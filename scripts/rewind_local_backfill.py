"""Rewind a local research backfill cursor without deleting downloaded rows."""
import argparse
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("last_date")
    parser.add_argument("--db", default="data/research/research.db")
    args = parser.parse_args()
    with sqlite3.connect(args.db) as conn:
        conn.execute(
            "UPDATE backfill_progress SET last_date=? WHERE task=?",
            (args.last_date, args.task),
        )
        row = conn.execute(
            "SELECT task, last_date FROM backfill_progress WHERE task=?",
            (args.task,),
        ).fetchone()
    print(row)


if __name__ == "__main__":
    main()
