import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "borrowboard.db"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # 每个用例独立库：清掉已导入模块缓存，让 startup 重新建库
    for m in [m for m in list(sys.modules) if m.startswith("app.")]:
        del sys.modules[m]
    from app.main import app
    from app.db import db_path
    assert db_path() == db_file
    with TestClient(app) as cl:
        # 清掉种子样例，每个用例从空集合开始，数量断言才有意义
        cl.app  # noqa: B018
        import sqlite3
        cc = sqlite3.connect(db_file)
        cc.execute("DELETE FROM loans")
        cc.execute("DELETE FROM items")
        cc.commit()
        cc.close()
        yield cl


def add_item(client, title="测试物", owner="老周"):
    return client.post("/api/items", json={"title": title, "owner": owner}).json()["id"]


def lend(client, iid, borrower="邻居甲", due="2026-12-31"):
    r = client.post(f"/api/items/{iid}/lend", json={"borrower": borrower, "due_date": due})
    assert r.status_code == 200, r.text
    return r.json()["loan_id"]


def state(client):
    """返回 items/loans 的完整状态，用于失败路径前后比对。"""
    import sqlite3
    from app.db import db_path
    c = sqlite3.connect(db_path())
    c.row_factory = sqlite3.Row
    items = [dict(r) for r in c.execute("SELECT id,status FROM items ORDER BY id")]
    loans = [dict(r) for r in c.execute(
        "SELECT id,item_id,borrower,status,rev_conflict,returned_at FROM loans ORDER BY id")]
    c.close()
    return {"items": items, "loans": loans}


def test_unreturn_success_is_atomic_and_board_consistent(client):
    iid = add_item(client)
    lid = lend(client, iid)
    assert client.post(f"/api/loans/{lid}/return").status_code == 200
    b = client.get("/api/board").json()
    assert b["counts"]["available"] == 1 and b["counts"]["active"] == 0

    r = client.post(f"/api/loans/{lid}/unreturn", json={"reason": "误点归还"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["conflict"] is False and body["bumped_loan_ids"] == []

    s = state(client)
    loan = next(l for l in s["loans"] if l["id"] == lid)
    item = next(i for i in s["items"] if i["id"] == iid)
    assert loan["status"] == "active" and loan["returned_at"] is None
    assert item["status"] == "on_loan"

    b = client.get("/api/board").json()
    # 可借栏无该物、在借栏挂着该物、顶细条三者一致
    assert all(i["id"] != iid for i in b["available"])
    assert any(l["id"] == lid for l in b["active"])
    assert b["counts"] == {"available": 0, "active": 1, "overdue": 0}
    loans = client.get("/api/loans").json()
    assert any(l["id"] == lid for l in loans["active"])
    assert all(l["id"] != lid for l in loans["returned"])


def test_unreturn_missing_reason_changes_nothing(client):
    iid = add_item(client)
    lid = lend(client, iid)
    client.post(f"/api/loans/{lid}/return")
    before = state(client)

    for payload in ({"reason": ""}, {"reason": "   "}):
        r = client.post(f"/api/loans/{lid}/unreturn", json=payload)
        assert r.status_code == 400 and r.json()["detail"] == "reason_required"
        assert state(client) == before


def test_unreturn_conflict_fail_changes_nothing(client):
    iid = add_item(client)
    lid = lend(client, iid, borrower="邻居甲")
    client.post(f"/api/loans/{lid}/return")
    lid2 = lend(client, iid, borrower="邻居乙")  # 物被别人借走
    before = state(client)

    r = client.post(f"/api/loans/{lid}/unreturn",
                    json={"reason": "误还", "on_conflict": "fail"})
    assert r.status_code == 409 and r.json()["detail"] == "item_relent"
    assert state(client) == before

    # 回包失败后：原笔仍 returned，新借仍 active，item 仍 on_loan，看板无凭空增减
    s = state(client)
    assert next(l for l in s["loans"] if l["id"] == lid)["status"] == "returned"
    assert next(l for l in s["loans"] if l["id"] == lid2)["status"] == "active"
    b = client.get("/api/board").json()
    assert b["counts"]["available"] == 0 and b["counts"]["active"] == 1


def test_unreturn_bump_is_atomic(client):
    iid = add_item(client)
    lid = lend(client, iid, borrower="邻居甲")
    client.post(f"/api/loans/{lid}/return")
    lid2 = lend(client, iid, borrower="邻居乙")

    r = client.post(f"/api/loans/{lid}/unreturn",
                    json={"reason": "误还", "on_conflict": "bump"})
    assert r.status_code == 200, r.text
    assert r.json()["bumped_loan_ids"] == [lid2]

    s = state(client)
    assert next(l for l in s["loans"] if l["id"] == lid)["status"] == "active"
    bumped = next(l for l in s["loans"] if l["id"] == lid2)
    assert bumped["status"] == "cancelled" and bumped["rev_conflict"] == "bumped"
    assert next(i for i in s["items"] if i["id"] == iid)["status"] == "on_loan"

    b = client.get("/api/board").json()
    # 可借栏没有、在借栏是原笔、被挤掉的笔不出现在在借栏
    assert b["counts"] == {"available": 0, "active": 1, "overdue": 0}
    assert [l["id"] for l in b["active"]] == [lid]
    loans = client.get("/api/loans").json()
    assert [l["id"] for l in loans["cancelled"]] == [lid2]


def test_unreturn_not_latest(client):
    iid = add_item(client)
    lid1 = lend(client, iid, borrower="甲")
    client.post(f"/api/loans/{lid1}/return")
    iid2 = add_item(client, title="另一物")
    lid2 = lend(client, iid2, borrower="乙")
    client.post(f"/api/loans/{lid2}/return")
    before = state(client)

    r = client.post(f"/api/loans/{lid1}/unreturn", json={"reason": "误还"})
    assert r.status_code == 409 and r.json()["detail"] == "not_latest_return"
    assert state(client) == before


def test_double_lend_only_one_wins(client):
    iid = add_item(client)
    lid = lend(client, iid)
    r = client.post(f"/api/items/{iid}/lend", json={"borrower": "邻居乙", "due_date": "2026-12-31"})
    assert r.status_code == 409
    s = state(client)
    actives = [l for l in s["loans"] if l["item_id"] == iid and l["status"] == "active"]
    assert [l["id"] for l in actives] == [lid]
    assert next(i for i in s["items"] if i["id"] == iid)["status"] == "on_loan"


def test_double_return_only_first_applies(client):
    iid = add_item(client)
    lid = lend(client, iid)
    assert client.post(f"/api/loans/{lid}/return").status_code == 200
    r = client.post(f"/api/loans/{lid}/return")
    assert r.status_code == 400 and r.json()["detail"] == "not_active"
    assert next(i for i in state(client)["items"] if i["id"] == iid)["status"] == "available"


def test_startup_reconciles_drifted_db(tmp_path, monkeypatch):
    """旧故障遗留：item on_loan 但 loan 已 returned；以及一物两条 active。"""
    import sqlite3
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in [m for m in list(sys.modules) if m.startswith("app.")]:
        del sys.modules[m]
    from app.db import db_path, connect
    from app import seed
    seed.init_db()
    c = connect()
    # 拆掉唯一索引，模拟旧版本在无兜底时留下的脏数据
    c.execute("DROP INDEX IF EXISTS idx_loans_one_active")
    iid = c.execute("INSERT INTO items(title,owner,status,data_quality) VALUES ('x','o','available','clean')").lastrowid
    l1 = c.execute(
        "INSERT INTO loans(item_id,borrower,status,due_date,lent_at,returned_at) "
        "VALUES (?,?,?,?,?,?)", (iid, "甲", "returned", "2026-12-31", "t", "t")).lastrowid
    # 漂移：已归还却把 item 改成 on_loan
    c.execute("UPDATE items SET status='on_loan' WHERE id=?", (iid,))
    iid2 = c.execute("INSERT INTO items(title,owner,status,data_quality) VALUES ('y','o','on_loan','clean')").lastrowid
    a1 = c.execute("INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
                   (iid2, "乙", "active", "2026-12-31", "t")).lastrowid
    a2 = c.execute("INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
                   (iid2, "丙", "active", "2026-12-31", "t")).lastrowid
    c.commit(); c.close()

    seed.init_db()  # 重启收口
    c = sqlite3.connect(db_path()); c.row_factory = sqlite3.Row
    assert c.execute("SELECT status FROM items WHERE id=?", (iid,)).fetchone()["status"] == "available"
    rows = c.execute("SELECT id,status,rev_conflict FROM loans WHERE item_id=? ORDER BY id", (iid2,)).fetchall()
    actives = [r for r in rows if r["status"] == "active"]
    assert len(actives) == 1 and actives[0]["id"] == a2  # 留最新
    assert dict(rows[0])["status"] == "cancelled"
    # 唯一索引已生效：再插一条 active 必失败
    import pytest as _pt
    import sqlite3 as _sq
    with _pt.raises(_sq.IntegrityError):
        c.execute("INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
                  (iid2, "丁", "active", "2026-12-31", "t"))
    c.close()
