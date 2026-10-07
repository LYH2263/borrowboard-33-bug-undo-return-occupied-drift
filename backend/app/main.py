from datetime import date, datetime, timezone
from typing import Literal
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect, tx
from app.engines.borrow_rules import can_lend, classify_loans

app = FastAPI(title="Borrowboard", version="0.1.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup():
    seed.init_db()
    seed.reconcile_item_status()

@app.get("/api/health")
def health(): return {"ok": True, "project": "borrowboard"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/board")
def board():
    c = connect()
    available = [dict(r) for r in c.execute("SELECT * FROM items WHERE status='available'")]
    loans = [dict(r) for r in c.execute(
        """SELECT loans.*, items.title FROM loans JOIN items ON items.id=loans.item_id
           WHERE loans.status='active'""")]
    c.close()
    cls = classify_loans(loans, date.today().isoformat())
    return {
        "available": available,
        "active": cls["active"],
        "overdue": cls["overdue"],
        "counts": {"available": len(available), "active": len(cls["active"]), "overdue": len(cls["overdue"])},
    }

class ItemIn(BaseModel):
    title: str
    owner: str

@app.post("/api/items")
def add_item(body: ItemIn):
    with tx() as c:
        cur = c.execute("INSERT INTO items(title,owner,status,data_quality) VALUES (?,?,?,?)",
                        (body.title, body.owner, "available", "clean"))
        iid = cur.lastrowid
    return {"id": iid}

class LendIn(BaseModel):
    borrower: str
    due_date: str

@app.post("/api/items/{iid}/lend")
def lend(iid: int, body: LendIn):
    # BEGIN IMMEDIATE：与撤销/归还在入口排队，进事务后读到的 items/loans 必一致
    with tx() as c:
        item = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        if not item: raise HTTPException(404, "item")
        active = c.execute(
            "SELECT COUNT(*) c FROM loans WHERE item_id=? AND status='active'", (iid,)
        ).fetchone()["c"]
        check = can_lend(item["status"], active)
        if not check["ok"]:
            raise HTTPException(409, check["reason"])
        now = datetime.now(timezone.utc).isoformat()
        cur = c.execute(
            "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
            (iid, body.borrower, "active", body.due_date, now))
        c.execute("UPDATE items SET status='on_loan' WHERE id=?", (iid,))
        lid = cur.lastrowid
    return {"loan_id": lid}

@app.post("/api/loans/{lid}/return")
def return_loan(lid: int):
    with tx() as c:
        loan = c.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
        if not loan: raise HTTPException(404, "loan")
        if loan["status"] != "active":
            raise HTTPException(400, "not_active")
        now = datetime.now(timezone.utc).isoformat()
        c.execute("UPDATE loans SET status='returned', returned_at=? WHERE id=?", (now, lid))
        c.execute("UPDATE items SET status='available' WHERE id=?", (loan["item_id"],))
    return {"ok": True}

class UnreturnIn(BaseModel):
    reason: str = ""
    on_conflict: Literal["fail", "bump"] = "fail"

def _latest_returned_check(c, loan) -> None:
    """最近一笔成功归还按 returned_at 判定（id 序在撤销/挤掉后不可靠）。"""
    row = c.execute(
        "SELECT MAX(returned_at) m FROM loans WHERE status='returned'"
    ).fetchone()
    latest = row["m"] if row and row["m"] is not None else ""
    if not loan["returned_at"] or loan["returned_at"] < latest:
        raise HTTPException(409, "not_latest_return")

@app.get("/api/loans/{lid}/unreturn/preview")
def unreturn_preview(lid: int):
    """只读预览：不改 items/loans，不影响左右分栏与顶细条之外的任何状态。"""
    c = connect()
    try:
        loan = c.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
        if not loan: raise HTTPException(404, "loan")
        active = [dict(r) for r in c.execute(
            "SELECT id,borrower,due_date FROM loans WHERE item_id=? AND status='active' "
            "ORDER BY id", (loan["item_id"],))]
        row = c.execute(
            "SELECT MAX(returned_at) m FROM loans WHERE status='returned'").fetchone()
        latest = row["m"] if row and row["m"] is not None else ""
        code = None
        if loan["status"] != "returned":
            code = "not_returned"
        elif not loan["returned_at"] or loan["returned_at"] < latest:
            code = "not_latest_return"
        return {
            "loan_id": lid,
            "item_id": loan["item_id"],
            "can_unreturn": code is None,
            "reason_code": code,
            "conflict": len(active) > 0,
            "active_loans": active,
        }
    finally:
        c.close()

@app.post("/api/loans/{lid}/unreturn")
def unreturn_loan(lid: int, body: UnreturnIn):
    reason = body.reason.strip() if isinstance(body.reason, str) else ""
    # 缺原因在开事务前拒绝：绝不触碰集合
    if not reason:
        raise HTTPException(400, "reason_required")
    bumped_ids = []
    with tx() as c:
        loan = c.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
        if not loan: raise HTTPException(404, "loan")
        if loan["status"] != "returned": raise HTTPException(400, "not_returned")
        _latest_returned_check(c, loan)
        iid = loan["item_id"]
        active = c.execute(
            "SELECT * FROM loans WHERE item_id=? AND status='active' ORDER BY id",
            (iid,)).fetchall()
        conflict = len(active) > 0
        if conflict and body.on_conflict == "fail":
            # 整单失败保持现况：只 raise，上下文管理器负责 rollback，items/loans 一字节不改
            raise HTTPException(409, "item_relent")
        now = datetime.now(timezone.utc).isoformat()
        if conflict:
            # bump：挤掉归还后被别人借出的在借笔（并发遗留也在此一并收口）
            for n in active:
                c.execute(
                    "UPDATE loans SET status='cancelled', unreturn_reason=?, rev_conflict='bumped' "
                    "WHERE id=?",
                    (f"撤销归还#{lid} 挤掉新借：{reason}", n["id"]))
                bumped_ids.append(n["id"])
        # 有冲突（bump）或无冲突，落定后 item 都应为 on_loan；loan 行回到 active
        c.execute("UPDATE items SET status='on_loan' WHERE id=?", (iid,))
        c.execute(
            "UPDATE loans SET status='active', returned_at=NULL, unreturned_at=?, "
            "unreturn_reason=?, rev_conflict=? WHERE id=?",
            (now, reason, "bump" if conflict else None, lid))
    return {"ok": True, "conflict": conflict, "bumped_loan_ids": bumped_ids}

@app.get("/api/loans")
def loans():
    c = connect()
    rows = [dict(r) for r in c.execute(
        "SELECT loans.*, items.title FROM loans JOIN items ON items.id=loans.item_id ORDER BY loans.id DESC")]
    c.close()
    return classify_loans(rows, date.today().isoformat())

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
