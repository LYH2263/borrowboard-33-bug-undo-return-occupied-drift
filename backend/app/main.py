from datetime import date, datetime, timezone
from typing import Literal
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.borrow_rules import can_lend, classify_loans
from app.engines import unreturn_drift as ud

app = FastAPI(title="Borrowboard", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

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
    c = connect()
    cur = c.execute("INSERT INTO items(title,owner,status,data_quality) VALUES (?,?,?,?)",
                    (body.title, body.owner, "available", "clean"))
    c.commit(); iid = cur.lastrowid; c.close(); return {"id": iid}

class LendIn(BaseModel):
    borrower: str
    due_date: str

@app.post("/api/items/{iid}/lend")
def lend(iid: int, body: LendIn):
    c = connect()
    item = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    active = c.execute("SELECT COUNT(*) c FROM loans WHERE item_id=? AND status='active'", (iid,)).fetchone()["c"]
    check = can_lend(item["status"], active)
    if not check["ok"]:
        c.close(); raise HTTPException(409, check["reason"])
    cur = c.execute(
        "INSERT INTO loans(item_id,borrower,status,due_date,lent_at) VALUES (?,?,?,?,?)",
        (iid, body.borrower, "active", body.due_date, datetime.now(timezone.utc).isoformat()))
    c.execute("UPDATE items SET status='on_loan' WHERE id=?", (iid,))
    c.commit(); lid = cur.lastrowid; c.close(); return {"loan_id": lid}

@app.post("/api/loans/{lid}/return")
def return_loan(lid: int):
    c = connect()
    loan = c.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
    if not loan: c.close(); raise HTTPException(404, "loan")
    if loan["status"] != "active":
        c.close(); raise HTTPException(400, "not_active")
    c.execute("UPDATE loans SET status='returned', returned_at=? WHERE id=?",
              (datetime.now(timezone.utc).isoformat(), lid))
    c.execute("UPDATE items SET status='available' WHERE id=?", (loan["item_id"],))
    c.commit(); c.close(); return {"ok": True}

class UnreturnIn(BaseModel):
    reason: str = ""
    on_conflict: Literal["fail", "bump"] = "fail"

@app.post("/api/loans/{lid}/unreturn")
def unreturn_loan(lid: int, body: UnreturnIn):
    reason = body.reason.strip() if isinstance(body.reason, str) else ""
    if not reason:
        raise HTTPException(400, "reason_required")
    c = connect()
    try:
        loan = c.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
        if not loan: raise HTTPException(404, "loan")
        if loan["status"] != "returned": raise HTTPException(400, "not_returned")
        # 只能撤销“最近一次成功 returned”：之后不得再有任何归还（全局最近一笔）
        newer = c.execute(
            "SELECT id FROM loans WHERE status='returned' AND id>?", (lid,)
        ).fetchone()
        if newer: raise HTTPException(409, "not_latest_return")
        iid = loan["item_id"]
        item = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        active = c.execute(
            "SELECT * FROM loans WHERE item_id=? AND status='active' ORDER BY id DESC",
            (iid,)).fetchall()
        conflict = len(active) > 0
        if conflict and body.on_conflict == "fail":
            ud.apply_fail_drift(c, iid)
            raise HTTPException(409, "item_relent")
        bumped_ids = []
        if conflict:
            # bump：挤掉归还后被别人借出的在借笔（单笔或并发遗留也一并收口）
            for n in active:
                c.execute(
                    "UPDATE loans SET status='cancelled', unreturn_reason=?, rev_conflict='bumped' "
                    "WHERE id=?",
                    (f"撤销归还#{lid} 挤掉新借：{reason}", n["id"]))
                bumped_ids.append(n["id"])
            c.execute("UPDATE items SET status='on_loan' WHERE id=?", (iid,))
        else:
            # 无冲突：item 必须确为 available（逾期扫只判 loan 行，不写 items.status）
            c.execute("UPDATE items SET status='on_loan' WHERE id=?", (iid,))
        c.execute(
            "UPDATE loans SET status='active', returned_at=NULL, unreturned_at=?, "
            "unreturn_reason=?, rev_conflict=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), reason,
             "bump" if conflict else None, lid))
        c.commit()
        return {"ok": True, "conflict": conflict, "bumped_loan_ids": bumped_ids}
    finally:
        c.close()

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
