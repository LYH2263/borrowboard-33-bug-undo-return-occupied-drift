import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "borrowboard.db"

def connect():
    c = sqlite3.connect(db_path(), timeout=5)
    c.row_factory = sqlite3.Row
    # 撤销归还与借出/逾期查看叠单时，写事务排队而不是立刻抛 database is locked
    c.execute("PRAGMA busy_timeout=5000")
    return c
