from app.db import connect

def _columns(c, table: str) -> set[str]:
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}

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
    c.close()

def reconcile_item_status():
    """启动对账：修掉旧版本失败路径提交/叠单留下的 items.status 与 loans 不一致。
    - 同一物若有多笔 active（旧版本借出 TOCTOU 叠单），保留最新一笔，其余收口为
      cancelled；有 active 笔 item 必为 on_loan；
    - 无 active 笔 item 必为 available（逾期只在读取时判，不会把 item 留在 on_loan）。"""
    c = connect()
    c.execute("BEGIN IMMEDIATE")
    c.execute(
        "UPDATE loans SET status='cancelled', rev_conflict='reconciled', "
        "unreturn_reason=COALESCE(unreturn_reason,'启动对账收口叠单') "
        "WHERE status='active' AND id NOT IN ("
        "SELECT MAX(id) FROM loans WHERE status='active' GROUP BY item_id)")
    c.execute(
        "UPDATE items SET status='on_loan' WHERE status!='on_loan' AND EXISTS ("
        "SELECT 1 FROM loans WHERE loans.item_id=items.id AND loans.status='active')")
    c.execute(
        "UPDATE items SET status='available' WHERE status='on_loan' AND NOT EXISTS ("
        "SELECT 1 FROM loans WHERE loans.item_id=items.id AND loans.status='active')")
    c.commit()
    c.close()
