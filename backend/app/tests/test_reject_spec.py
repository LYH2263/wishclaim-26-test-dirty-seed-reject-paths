"""脏种与非法态拒写规格用例(自备脏愿望夹具)。

与矩阵测例包 test_claim_lock.py 分文件存放;本文件自备夹具,不读矩阵包表格行。
每条断言带标签(ACx#y:...),失败时 pytest 直接打印标签。
双通道原则:HTTP 通道与引擎/状态通道必须同钉,
不允许「仅 HTTP 成功而引擎失败」(或反之)。
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import seed
from app.db import connect
from app.engines.claim_lock import claim_allowed
from app.main import app

ALICE = "alice"  # 原 claimer
BOB = "bob"      # 换人后的新 claimer
GHOST = "ghost"  # 过期锁的占位 claimer

# 自备脏种夹具行: (title, note, status, claimer, claimed_at, expires_at, data_quality)
# claimed_at/expires_at 在夹具里按真实当前时间现算,避免依赖固定时钟。
T_OPEN = "干净-开放"
T_HOTLOCK = "干净-热锁"
T_EXPLOCK = "脏-过期锁"
T_EMPTY = "脏-空标题"
T_BADST = "脏-非法状态"
T_DONE = "已完成"
T_RELEASED = "已释放"


def _iso(dt):
    return dt.isoformat()


def _fixture_rows():
    now = datetime.now(timezone.utc)
    past = _iso(now - timedelta(hours=2))
    future = _iso(now + timedelta(hours=2))
    return [
        (T_OPEN, "可被认领", "open", None, None, None, "clean"),
        (T_HOTLOCK, "alice 未过期锁", "claimed", ALICE, past, future, "clean"),
        (T_EXPLOCK, "ghost 锁已过期应被TTL释放", "claimed", GHOST, past, past, "dirty"),
        (T_EMPTY, "", "open", None, None, None, "dirty"),
        (T_BADST, "引擎应判 bad_status", "weird", None, None, None, "dirty"),
        (T_DONE, "alice 已核销", "fulfilled", ALICE, past, None, "clean"),
        (T_RELEASED, "释放后可换人认领", "released", None, None, None, "clean"),
    ]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """自备脏愿望夹具:独立 tmp 库 + 自建脏种行,不依赖 seed 默认种子。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()  # 建表 + 默认 settings(ttl_seconds 等)
    c = connect()
    c.execute("DELETE FROM wishes")  # 清掉默认种子,只留自备脏种
    c.executemany(
        "INSERT INTO wishes(title,note,status,claimer,claimed_at,expires_at,data_quality)"
        " VALUES (?,?,?,?,?,?,?)",
        _fixture_rows(),
    )
    c.commit()
    ids = {r["title"]: r["id"] for r in c.execute("SELECT id,title FROM wishes")}
    c.close()
    with TestClient(app) as client:
        yield client, ids


def _count_wishes():
    c = connect()
    n = c.execute("SELECT COUNT(*) n FROM wishes").fetchone()["n"]
    c.close()
    return n


def _row(wid):
    c = connect()
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    c.close()
    return dict(r) if r else None


def test_ac1_dirty_or_empty_title_create_rejected(world):
    """AC1: dirty/空标题创建失败,且表不增行,且墙列表不得出现该标题。"""
    client, _ids = world
    before = _count_wishes()
    for bad in ("", "   ", "\n\t "):
        resp = client.post("/api/wishes", json={"title": bad, "note": "脏写"})
        assert resp.status_code == 400, (
            f"AC1#create-rejected: 空/脏标题 {bad!r} 应被 400 拒写, 实得 {resp.status_code}"
        )
    assert _count_wishes() == before, (
        f"AC1#table-not-grown: 拒写后 wishes 表不得增行(前 {before}, 后 {_count_wishes()})"
    )
    wall = client.get("/api/wishes").json()
    titles = [w["title"] for w in wall]
    for bad in ("", "   ", "\n\t "):
        assert bad not in titles, f"AC1#wall-clean: 墙列表不得出现被拒标题 {bad!r}"
    assert len(wall) == before, "AC1#wall-count: 墙列表行数不得因拒写而变化"


def test_ac2_fulfill_on_open_rejected(world):
    """AC2: 对 open 调 fulfill 失败,status 不变,已完成页不增行。"""
    client, ids = world
    wid = ids[T_OPEN]
    row_before = _row(wid)
    done_before = len(client.get("/api/done").json())
    resp = client.post(f"/api/wishes/{wid}/fulfill")
    assert resp.status_code == 400, (
        f"AC2#fulfill-rejected: open 不得核销, 应 400, 实得 {resp.status_code}"
    )
    assert _row(wid)["status"] == "open", "AC2#status-unchanged: 拒核销后 status 必须仍是 open"
    assert _row(wid) == row_before, "AC2#row-untouched: 拒写后该行任何字段不得被改写"
    done_after = len(client.get("/api/done").json())
    assert done_after == done_before, (
        f"AC2#done-not-grown: 已完成页不得增行(前 {done_before}, 后 {done_after})"
    )
    eng = claim_allowed("open", None, datetime.now(timezone.utc), None)
    assert eng["ok"] is True, "AC2#engine-state-consistent: open 行在引擎通道必须仍可认领"


def test_ac3_claim_on_fulfilled_rejected(world):
    """AC3: 对 fulfilled 再 claim 失败,墙仍钉原 claimer。"""
    client, ids = world
    wid = ids[T_DONE]
    resp = client.post(f"/api/wishes/{wid}/claim", json={"claimer": BOB})
    assert resp.status_code == 409, (
        f"AC3#claim-rejected: fulfilled 不得再认领, 应 409, 实得 {resp.status_code}"
    )
    eng = claim_allowed("fulfilled", ALICE, datetime.now(timezone.utc), None)
    assert eng["ok"] is False and eng["reason"] == "already_fulfilled", (
        f"AC3#engine-agrees: 引擎通道必须同钉 already_fulfilled, 实得 {eng}"
    )
    wall = {w["id"]: w for w in client.get("/api/wishes").json()}
    assert wall[wid]["claimer"] == ALICE, "AC3#wall-pins-original: 墙上仍钉原 claimer"
    assert wall[wid]["status"] == "fulfilled", "AC3#wall-status: 墙上状态仍为 fulfilled"
    assert _row(wid)["claimer"] == ALICE, "AC3#db-pins-original: 库中 claimer 未被改写"


def test_ac4_release_then_reclaim_by_other(world):
    """AC4: release 后换人 claim 成功,墙/详情/mine 三路都钉新人。"""
    client, ids = world
    wid = ids[T_HOTLOCK]
    r1 = client.post(f"/api/wishes/{wid}/release")
    assert r1.status_code == 200, f"AC4#release-ok: 释放应成功, 实得 {r1.status_code}"
    assert _row(wid)["status"] == "released", "AC4#released-state: 释放后库状态须为 released"
    eng = claim_allowed("released", None, datetime.now(timezone.utc), None)
    assert eng["ok"] is True, f"AC4#engine-allows: 引擎必须允许 released 再认领, 实得 {eng}"
    r2 = client.post(f"/api/wishes/{wid}/claim", json={"claimer": BOB})
    assert r2.status_code == 200, f"AC4#reclaim-ok: 换人认领应成功, 实得 {r2.status_code}"
    wall = {w["id"]: w for w in client.get("/api/wishes").json()}
    assert wall[wid]["claimer"] == BOB, "AC4#wall-new-claimer: 墙必须钉新 claimer"
    detail = client.get(f"/api/wishes/{wid}").json()
    assert detail["claimer"] == BOB, "AC4#detail-new-claimer: 详情必须钉新 claimer"
    mine_bob = [w["id"] for w in client.get("/api/mine", params={"claimer": BOB}).json()]
    mine_alice = [w["id"] for w in client.get("/api/mine", params={"claimer": ALICE}).json()]
    assert wid in mine_bob, "AC4#mine-new-claimer: mine 新人必须可见该行"
    assert wid not in mine_alice, "AC4#mine-old-cleared: mine 旧人不得再见该行"


def test_ac5_dual_channel_same_pin(world):
    """AC5: 双通道同钉——每种状态下 HTTP 结果与引擎判定必须一致,
    不得出现「仅 HTTP 成功而引擎失败」;拒写时库行零改动,放行时库行必落写。"""
    client, ids = world
    now = datetime.now(timezone.utc)
    for title in (T_OPEN, T_HOTLOCK, T_EXPLOCK, T_DONE, T_RELEASED, T_BADST):
        wid = ids[title]
        before = _row(wid)
        eng = claim_allowed(before["status"], before["claimer"], now, before["expires_at"])
        resp = client.post(f"/api/wishes/{wid}/claim", json={"claimer": BOB})
        http_ok = resp.status_code == 200
        assert http_ok == eng["ok"], (
            f"AC5#dual-channel[{title}]: HTTP({resp.status_code}) 与引擎({eng}) 必须同钉"
        )
        after = _row(wid)
        if eng["ok"]:
            assert after["status"] == "claimed" and after["claimer"] == BOB, (
                f"AC5#write-applied[{title}]: 双通道放行后库必须落写为 bob 的 claimed"
            )
        else:
            assert after == before, (
                f"AC5#no-write[{title}]: 双通道拒写后库行不得有任何改动"
            )
