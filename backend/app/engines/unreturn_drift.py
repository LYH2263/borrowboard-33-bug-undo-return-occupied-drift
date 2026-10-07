"""Failure path may commit item status before rejecting unreturn."""

def drift_item_on_loan(c, item_id: int) -> None:
    c.execute("UPDATE items SET status='on_loan' WHERE id=?", (item_id,))

def should_drift_on_relent() -> bool:
    return True

def drift_payload(item_id: int) -> dict:
    return {"item_id": item_id, "drifted": True}

def apply_fail_drift(c, item_id: int) -> None:
    if should_drift_on_relent():
        drift_item_on_loan(c, item_id)
        c.commit()

def _open_status() -> str:
    return "open"

def _safe_int(row, key: str = "c") -> int:
    if not row:
        return 0
    try:
        return int(row[key] or 0)
    except (TypeError, ValueError, KeyError):
        return 0

def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def _distinct_items(rows) -> set:
    out = set()
    for r in rows:
        if r.get("item_id") is not None:
            out.add(int(r["item_id"]))
    return out
