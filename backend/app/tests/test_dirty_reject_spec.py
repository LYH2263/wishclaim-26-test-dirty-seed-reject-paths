"""脏种与非法态拒写规格用例 (dirty-seed / illegal-state rejection spec).

与矩阵包 (test_claim_lock.py) 分文件: 矩阵包只测纯引擎函数; 本文件测 HTTP
与引擎双通道在脏数据与非法状态迁移下必须同钉 (lockstep)。

- 自备脏愿望夹具 (fixtures), 不依赖 seed.py 的样例行;
- 每个断言带标签, 失败时打印断言标签;
- 可被 pytest 收集; 禁止去改矩阵那包的表格行。
"""

import os
import tempfile
from datetime import datetime, timezone

import pytest

# 在导入 app.* 之前指向临时库, 保证夹具完全自备、不触碰真实数据文件;
# 每个测例的 client 夹具还会用各自的 tmp_path 再隔离一次。
_TMP_DATA_DIR = tempfile.mkdtemp(prefix="wishclaim_spec_")
os.environ["DATA_DIR"] = _TMP_DATA_DIR

from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.db import connect  # noqa: E402
from app.engines.claim_lock import claim_allowed  # noqa: E402
from app.main import app as api_app  # noqa: E402


# ---------------------------------------------------------------------------
# 小工具: 每步用独立连接直读 SQLite, 与 HTTP 通道互为旁证 (双通道同钉)
# ---------------------------------------------------------------------------

def _row(wid):
    c = connect()
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    c.close()
    return dict(r) if r else None


def _count_rows():
    c = connect()
    n = c.execute("SELECT COUNT(*) FROM wishes").fetchone()[0]
    c.close()
    return n


def _db_blank_titles():
    c = connect()
    rows = c.execute("SELECT id, title FROM wishes").fetchall()
    c.close()
    return [(r[0], r[1]) for r in rows if not (r[1] or "").strip()]


def _done_ids(client):
    """已完成页: GET /api/done 的 id 集合 (HTTP 通道)。"""
    return {w["id"] for w in client.get("/api/done").json()}


def _db_done_ids():
    """已完成页数据源: 表内 status='fulfilled' 的 id 集合 (引擎/DB 通道)。"""
    c = connect()
    ids = {r[0] for r in c.execute("SELECT id FROM wishes WHERE status='fulfilled'")}
    c.close()
    return ids


# ---------------------------------------------------------------------------
# 夹具: 每测例独立临时库 + 自备脏种
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    """隔离库: 每测例皆为独立临时目录下的 wishclaim.db。

    仅借 seed.init_db() 建表/置设置; 种子样例愿望一律清空 —— 本规格的
    脏愿望全部由下方夹具自备, 不依赖、也不改动任何矩阵包表格行。
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    c = connect()
    c.execute("DELETE FROM wishes")
    c.commit()
    c.close()
    # 应用 startup 会再调一次 seed.init_db(); 库已空会被重新塞入含空标题的
    # 样例脏行, 故置空, 保证本规格的脏种全部来自自备夹具。
    monkeypatch.setattr(seed, "init_db", lambda: None)
    with TestClient(api_app) as cl:
        yield cl


@pytest.fixture
def dirty_open_wish(client):
    """自备脏愿望: data_quality='dirty' 的历史遗留 open 行, 直接走 SQL 种入,
    模拟脏数据与正常写入共存; 返回 id。"""
    c = connect()
    cur = c.execute(
        "INSERT INTO wishes(title,note,status,claimer,claimed_at,expires_at,data_quality)"
        " VALUES ('脏愿望-历史遗留','字段缺失的遗留数据','open',NULL,NULL,NULL,'dirty')"
    )
    wid = cur.lastrowid
    c.commit()
    c.close()
    return wid


@pytest.fixture
def claimed_wish(client):
    """干净愿望被 alice 认领 (锁未过期), 供非法 fulfill / 合法 release 场景。"""
    wid = client.post("/api/wishes", json={"title": "热可可", "note": "棉花糖"}).json()["id"]
    client.post(f"/api/wishes/{wid}/claim", json={"claimer": "alice"})
    return wid


# ===========================================================================
# 断言 1: dirty / 空标题创建失败, 表不增行, 墙列表不出现该标题
# ===========================================================================

def test_blank_title_create_is_rejected(client, dirty_open_wish):
    L = "A1_blank_title_rejected_no_row_not_on_wall"
    dirty_id = dirty_open_wish
    before = _count_rows()

    r = client.post("/api/wishes", json={"title": "   ", "note": "只有空白的标题"})

    # (a) HTTP 通道: 必须失败
    assert r.status_code in (400, 422), f"[{L}] HTTP 应拒绝空白标题, 实际 {r.status_code}: {r.text}"

    # (b) 引擎/DB 通道: 表不得增行, 自备脏行原样保留
    after = _count_rows()
    assert after == before, f"[{L}] 拒绝创建后表行数 {before} -> {after}, 不应增行"
    kept = _row(dirty_id)
    assert kept is not None and kept["data_quality"] == "dirty" and kept["status"] == "open", \
        f"[{L}] 拒写波及自备脏行: {kept}"

    # (c) 墙列表 (GET /api/wishes) 中不得出现该空白标题行 (脏行标题非空, 可正常在墙)
    wall = client.get("/api/wishes").json()
    blank = [w for w in wall if not (w.get("title") or "").strip()]
    assert not blank, f"[{L}] 墙列表出现空标题行: {blank}"
    assert dirty_id in {w["id"] for w in wall}, f"[{L}] 自备脏行应仍在墙上"

    # (d) 双通道同钉: DB 侧同样不得有空标题行
    assert not _db_blank_titles(), f"[{L}] DB 通道出现空标题行: {_db_blank_titles()}"


# ===========================================================================
# 断言 2: 对 open 调 fulfill 失败, status 不变, 已完成页不增行
# ===========================================================================

def test_fulfill_on_open_is_rejected(client):
    L = "A2_fulfill_open_rejected_status_unchanged_not_done"
    wid = client.post("/api/wishes", json={"title": "手套", "note": ""}).json()["id"]

    r = client.post(f"/api/wishes/{wid}/fulfill")

    # (a) HTTP 失败
    assert r.status_code in (400, 409), f"[{L}] open 行 fulfill 应失败, 实际 {r.status_code}: {r.text}"
    # (b) DB 状态不变
    row = _row(wid)
    assert row["status"] == "open", f"[{L}] 拒写后 status 被改成 {row['status']}"
    assert row["claimer"] is None, f"[{L}] 拒写后 claimer 被污染: {row['claimer']}"
    # (c) 已完成页不增行 —— HTTP /api/done
    assert wid not in _done_ids(client), f"[{L}] HTTP 已完成页出现未认领行 {wid}"
    # (d) 双通道同钉: DB 侧也无 fulfilled
    assert wid not in _db_done_ids(), f"[{L}] DB 通道出现 fulfilled 行 {wid}"
    assert _done_ids(client) == _db_done_ids(), f"[{L}] 双通道漂移: HTTP={_done_ids(client)} DB={_db_done_ids()}"


# ===========================================================================
# 断言 3: 对 fulfilled 再 claim 失败, 墙仍钉原 claimer
# ===========================================================================

def test_claim_fulfilled_is_rejected(client, claimed_wish):
    L = "A3_claim_fulfilled_rejected_original_claimer_pinned"
    wid = claimed_wish
    assert client.post(f"/api/wishes/{wid}/fulfill").status_code == 200

    r = client.post(f"/api/wishes/{wid}/claim", json={"claimer": "mallory"})

    # (a) HTTP 拒绝
    assert r.status_code == 409, f"[{L}] fulfilled 行再 claim 应 409, 实际 {r.status_code}: {r.text}"
    # (b) 墙通道: GET /api/wishes 中该行仍钉原 claimer / 状态不变
    wall = {w["id"]: w for w in client.get("/api/wishes").json()}
    assert wall[wid]["status"] == "fulfilled", f"[{L}] 墙状态被改: {wall[wid]['status']}"
    assert wall[wid]["claimer"] == "alice", f"[{L}] 墙 claimer 被换成 {wall[wid]['claimer']}"
    # (c) 详情通道同钉
    d = client.get(f"/api/wishes/{wid}").json()
    assert d["claimer"] == "alice" and d["status"] == "fulfilled", f"[{L}] 详情通道漂移: {d}"
    # (d) DB 通道同钉
    row = _row(wid)
    assert row["claimer"] == "alice" and row["status"] == "fulfilled", f"[{L}] DB 通道漂移: {row}"


# ===========================================================================
# 断言 4: release 后的行换人 claim 成功, 墙/详情/mine 三路皆新人
# ===========================================================================

def test_reassign_after_release_succeeds_three_channels(client, claimed_wish):
    L = "A4_release_then_reclaim_wall_detail_mine_new_claimer"
    wid = claimed_wish

    rl = client.post(f"/api/wishes/{wid}/release")
    assert rl.status_code == 200 and rl.json().get("status") == "released", f"[{L}] release 失败: {rl.text}"

    rc = client.post(f"/api/wishes/{wid}/claim", json={"claimer": "carol"})
    # (a) 换人认领成功
    assert rc.status_code == 200, f"[{L}] release 后换人 claim 应成功, 实际 {rc.status_code}: {rc.text}"
    assert rc.json().get("claimer") == "carol", f"[{L}] 响应未钉新人: {rc.text}"

    # (b) 墙通道是新人
    wall = {w["id"]: w for w in client.get("/api/wishes").json()}
    assert wall[wid]["claimer"] == "carol" and wall[wid]["status"] == "claimed", \
        f"[{L}] 墙通道未钉新人: {wall[wid]}"
    # (c) 详情通道是新人
    d = client.get(f"/api/wishes/{wid}").json()
    assert d["claimer"] == "carol" and d["status"] == "claimed", f"[{L}] 详情通道未钉新人: {d}"
    # (d) mine 通道: carol 的 mine 有此单, alice 的 mine 无此单
    carols = {w["id"]: w for w in client.get("/api/mine", params={"claimer": "carol"}).json()}
    alices = {w["id"]: w for w in client.get("/api/mine", params={"claimer": "alice"}).json()}
    assert wid in carols and carols[wid]["claimer"] == "carol", f"[{L}] carol 的 mine 未钉: {carols}"
    assert wid not in alices, f"[{L}] alice 的 mine 仍残留 {wid}: {alices}"
    # (e) 双通道同钉: DB 行也是 carol
    assert _row(wid)["claimer"] == "carol", f"[{L}] DB 通道 claimer 不是 carol: {_row(wid)}"


# ===========================================================================
# 断言 5: 不得出现「仅 HTTP 成功而引擎失败」—— 双通道必须同钉
# ===========================================================================

def test_http_and_engine_lockstep_on_illegal_claim(client, claimed_wish):
    L = "A5_http_engine_lockstep_no_http_ok_engine_fail"
    wid = claimed_wish  # alice 持锁且未过期

    r = client.post(f"/api/wishes/{wid}/claim", json={"claimer": "bob"})

    # 引擎纯函数是拒写规则的唯一事实源: 它必须与 HTTP 层同钉
    row = _row(wid)
    engine = claim_allowed(row["status"], row["claimer"], datetime.now(timezone.utc), row["expires_at"])

    # (a) 引擎先判: 必须拒
    assert engine["ok"] is False, f"[{L}] 引擎竟放行被锁行: {engine}"
    # (b) HTTP 必须同钉拒绝 (不允许 2xx 但引擎失败, 亦不允许引擎拒而 HTTP 写)
    assert r.status_code == 409, f"[{L}] HTTP 未与引擎同钉, 实际 {r.status_code}: {r.text}"
    # (c) DB 行未被 bob 覆写
    again = _row(wid)
    assert again["claimer"] == "alice", f"[{L}] HTTP 表面拒但 DB 被写: {again}"
    assert again["status"] == "claimed", f"[{L}] DB 状态漂移: {again['status']}"
    # (d) 反向校验: 引擎放行的迁移 (released -> claim) HTTP 也必须成功, 两侧真值一致
    assert client.post(f"/api/wishes/{wid}/release").status_code == 200
    row2 = _row(wid)
    engine2 = claim_allowed(row2["status"], row2["claimer"], datetime.now(timezone.utc), row2["expires_at"])
    assert engine2["ok"] is True, f"[{L}] released 行引擎应放行: {engine2}"
    r2 = client.post(f"/api/wishes/{wid}/claim", json={"claimer": "bob"})
    assert r2.status_code == 200, f"[{L}] 引擎放行但 HTTP 拒绝, 双通道漂移: {r2.status_code}"
    assert _row(wid)["claimer"] == "bob", f"[{L}] 引擎/HTTP 成功但 DB 未钉 bob: {_row(wid)}"
