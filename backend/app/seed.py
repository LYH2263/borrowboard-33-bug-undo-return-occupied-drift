from app.db import connect

def _columns(c, table: str) -> set[str]:
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}

def _reconcile(c) -> None:
    """建唯一索引前收口历史漂移：items.status 以是否存在 active loan 为准；
    一物多条 active（旧故障/并发遗留）只留最新一笔，其余记为 cancelled。"""
    dupes = c.execute(
        "SELECT item_id, COUNT(*) c FROM loans WHERE status='active' GROUP BY item_id HAVING c>1"
    ).fetchall()
    for d in dupes:
        c.execute(
            "UPDATE loans SET status='cancelled', rev_conflict='reconciled' "
            "WHERE item_id=? AND status='active' AND id NOT IN "
            "(SELECT MAX(id) FROM loans WHERE item_id=? AND status='active')",
            (d["item_id"], d["item_id"]))
    c.execute(
        "UPDATE items SET status='on_loan' WHERE EXISTS "
        "(SELECT 1 FROM loans WHERE loans.item_id=items.id AND loans.status='active') "
        "AND status!='on_loan'"
    )
    c.execute(
        "UPDATE items SET status='available' WHERE NOT EXISTS "
        "(SELECT 1 FROM loans WHERE loans.item_id=items.id AND loans.status='active') "
        "AND status!='available'"
    )

def init_db():
    c = connect()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS items(
      id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, owner TEXT, status TEXT, data_quality TEXT
    );
    CREATE TABLE IF NOT EXISTS loans(
      id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INT, borrower TEXT, status TEXT,
      due_date TEXT, lent_at TEXT, returned_at TEXT,
      unreturned_at TEXT, unreturn_reason TEXT, rev_conflict TEXT
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    """)
    # 轻量迁移：既有库补撤销归还所需列
    loan_cols = _columns(c, "loans")
    for col, decl in (
        ("unreturned_at", "TEXT"),
        ("unreturn_reason", "TEXT"),
        ("rev_conflict", "TEXT"),
    ):
        if col not in loan_cols:
            c.execute(f"ALTER TABLE loans ADD COLUMN {col} {decl}")
    if c.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 0:
        c.executemany("INSERT INTO items(title,owner,status,data_quality) VALUES (?,?,?,?)", [
            ("电钻", "老周", "available", "clean"),
            ("折叠桌", "小陈", "available", "clean"),
            ("脏数据-无主", "", "available", "dirty"),
            ("已外借样例", "阿强", "on_loan", "clean"),
        ])
        c.execute(
            "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
            (4, "邻居甲", "active", "2020-06-01", "2020-05-01"),
        )
        c.execute("INSERT INTO settings(key,value) VALUES ('board_name','木色邻里板')")
        c.commit()
    # 收口旧故障留下的漂移，再建“一物一在借”部分唯一索引兜底叠单
    _reconcile(c)
    c.commit()
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_loans_one_active "
        "ON loans(item_id) WHERE status='active'"
    )
    c.commit()
    c.close()
