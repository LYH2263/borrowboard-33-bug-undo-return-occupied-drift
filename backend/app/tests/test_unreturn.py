import json
import os
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest
import uvicorn


def _http(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = {}
        return e.code, payload


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("data")
    os.environ["DATA_DIR"] = str(data_dir)
    from app.main import app  # imported after DATA_DIR is set
    config = uvicorn.Config(app, host="127.0.0.1", port=10477, log_level="error")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    base = "http://127.0.0.1:10477/api"
    for _ in range(100):
        try:
            _http("GET", base + "/health")
            break
        except OSError:
            time.sleep(0.05)
    yield base
    srv.should_exit = True


def _make_item(server, title):
    _, r = _http("POST", server + "/items", {"title": title, "owner": "t"})
    return r["id"]


def _lend(server, iid, borrower="邻居", due="2026-12-31"):
    _, r = _http("POST", f"{server}/items/{iid}/lend",
                 {"borrower": borrower, "due_date": due})
    return r["loan_id"]


def _snapshot(server):
    _, items = _http("GET", server + "/items")
    _, loans = _http("GET", server + "/loans")
    return items, loans


def _assert_invariants(server):
    """items.status 必须与 active loans 严格一致；每物至多一笔 active。"""
    _, items = _http("GET", server + "/items")
    _, loans = _http("GET", server + "/loans")
    active_count = {}
    for l in loans["active"] + loans["overdue"]:
        active_count[l["item_id"]] = active_count.get(l["item_id"], 0) + 1
    for it in items:
        n = active_count.get(it["id"], 0)
        assert n <= 1, f"item {it['id']} 有 {n} 笔 active"
        assert it["status"] == ("on_loan" if n else "available"), (
            f"item {it['id']} status={it['status']} 但 active={n}：可借栏与在借栏对不上")


def test_reason_required_changes_nothing(server):
    iid = _make_item(server, "缺原因测试")
    lid = _lend(server, iid)
    _http("POST", f"{server}/loans/{lid}/return", {})
    before = _snapshot(server)
    code, _ = _http("POST", f"{server}/loans/{lid}/unreturn",
                    {"reason": "   ", "on_conflict": "fail"})
    assert code == 400
    after = _snapshot(server)
    assert before == after, "缺原因失败不得改任何集合"


def test_fail_conflict_changes_nothing(server):
    iid = _make_item(server, "被人借走")
    lid = _lend(server, iid, borrower="甲")
    _http("POST", f"{server}/loans/{lid}/return", {})
    lid2 = _lend(server, iid, borrower="乙")  # 归还后被别人借出
    before = _snapshot(server)
    code, body = _http("POST", f"{server}/loans/{lid}/unreturn",
                       {"reason": "拿错了", "on_conflict": "fail"})
    assert code == 409 and body["detail"] == "item_relent"
    after = _snapshot(server)
    assert before == after, "fail 冲突处置失败不得改集合（item 不得漂移成 on_loan）"
    # 现况保持：新借仍 active，原笔仍 returned
    _, loans = _http("GET", server + "/loans")
    by_id = {l["id"]: l for grp in loans.values() for l in grp}
    assert by_id[lid2]["status"] == "active"
    assert by_id[lid]["status"] == "returned"
    _assert_invariants(server)


def test_bump_restores_and_cancels_new_loan(server):
    iid = _make_item(server, "挤掉测试")
    lid = _lend(server, iid, borrower="甲")
    _http("POST", f"{server}/loans/{lid}/return", {})
    lid2 = _lend(server, iid, borrower="乙")
    code, r = _http("POST", f"{server}/loans/{lid}/unreturn",
                    {"reason": "其实没还", "on_conflict": "bump"})
    assert code == 200 and r["conflict"] is True and r["bumped_loan_ids"] == [lid2]
    _, loans = _http("GET", server + "/loans")
    by_id = {l["id"]: l for grp in loans.values() for l in grp}
    assert by_id[lid]["status"] == "active"
    assert by_id[lid]["returned_at"] is None
    assert by_id[lid2]["status"] == "cancelled"
    _assert_invariants(server)


def test_latest_return_judged_by_returned_at_not_id(server):
    # 两笔在借；先还 id 更大的 B，再还 id 更小的 A —— A 才是最近一笔
    a = _make_item(server, "A物")
    b = _make_item(server, "B物")
    la = _lend(server, a, borrower="甲")
    lb = _lend(server, b, borrower="乙")
    _http("POST", f"{server}/loans/{lb}/return", {})
    time.sleep(0.02)
    _http("POST", f"{server}/loans/{la}/return", {})
    # 预览：B（先还）已不是最近一笔
    code, prev = _http("GET", f"{server}/loans/{lb}/unreturn/preview")
    assert code == 200 and prev["can_unreturn"] is False
    assert prev["reason_code"] == "not_latest_return"
    code, _ = _http("POST", f"{server}/loans/{lb}/unreturn", {"reason": "x"})
    assert code == 409
    # A 可正常撤销
    code, r = _http("POST", f"{server}/loans/{la}/unreturn", {"reason": "y"})
    assert code == 200 and r["conflict"] is False
    _assert_invariants(server)


def test_preview_is_read_only(server):
    iid = _make_item(server, "预览只读")
    lid = _lend(server, iid)
    _http("POST", f"{server}/loans/{lid}/return", {})
    before = _snapshot(server)
    code, prev = _http("GET", f"{server}/loans/{lid}/unreturn/preview")
    assert code == 200 and prev["can_unreturn"] is True and prev["conflict"] is False
    assert _snapshot(server) == before


def test_concurrent_unreturn_and_relend_never_splits(server):
    """撤销与新借出叠单 N 轮：不得留下 可借栏已加/在借栏仍挂 或其反面。"""
    iid = _make_item(server, "叠单物")
    barrier = threading.Barrier(2)
    outcomes = []

    for round_i in range(8):
        # 归位：上一轮撤销若跑赢，物合法在借，先归还再开始本轮
        _, board0 = _http("GET", server + "/board")
        onloan = next((l for l in board0["active"] + board0["overdue"]
                       if l["item_id"] == iid), None)
        if onloan:
            code, _ = _http("POST", f"{server}/loans/{onloan['id']}/return", {})
            assert code == 200
        lid = _lend(server, iid, borrower=f"轮{round_i}")
        _http("POST", f"{server}/loans/{lid}/return", {})

        def do_unreturn():
            barrier.wait()
            outcomes.append(("unreturn",) + _http(
                "POST", f"{server}/loans/{lid}/unreturn",
                {"reason": "并发撤销", "on_conflict": "bump"}))

        def do_lend():
            barrier.wait()
            outcomes.append(("lend",) + _http(
                "POST", f"{server}/items/{iid}/lend",
                {"borrower": f"并发客{round_i}", "due_date": "2026-12-31"}))

        t1 = threading.Thread(target=do_unreturn)
        t2 = threading.Thread(target=do_lend)
        t1.start(); t2.start(); t1.join(); t2.join()
        _assert_invariants(server)

        # 无论谁先谁后，收口到单一确定状态：
    # 终态再判一次 board 与 loans 合计对账
    _, board = _http("GET", server + "/board")
    _, loans = _http("GET", server + "/loans")
    board_active = {(l["item_id"], l["id"]) for l in board["active"] + board["overdue"]}
    rec_active = {(l["item_id"], l["id"]) for l in loans["active"] + loans["overdue"]}
    assert board_active == rec_active, "看板在借栏与借还记录在借集合不一致"
    assert board["counts"]["available"] == len(board["available"])


def test_concurrent_unreturn_fail_vs_lend(server):
    iid = _make_item(server, "fail叠单物")
    lid = _lend(server, iid)
    _http("POST", f"{server}/loans/{lid}/return", {})
    barrier = threading.Barrier(2)
    results = {}

    def do_unreturn():
        barrier.wait()
        results["unreturn"] = _http(
            "POST", f"{server}/loans/{lid}/unreturn",
            {"reason": "并发", "on_conflict": "fail"})

    def do_lend():
        barrier.wait()
        results["lend"] = _http(
            "POST", f"{server}/items/{iid}/lend",
            {"borrower": "后来者", "due_date": "2026-12-31"})

    t1 = threading.Thread(target=do_unreturn)
    t2 = threading.Thread(target=do_lend)
    t1.start(); t2.start(); t1.join(); t2.join()
    # 恰一个成功一个失败，且集合自洽；绝不两者都成功产生两笔 active
    codes = {k: v[0] for k, v in results.items()}
    assert sorted(codes.values()).count(200) == 1, codes
    _assert_invariants(server)
