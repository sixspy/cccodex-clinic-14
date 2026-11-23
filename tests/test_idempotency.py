"""幂等键败测：动笔改码前写定，先红后绿。

覆盖 docs/幂等键设计.md 第五节十条败测清单，全部直接打业务库。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from app.db import get_db
from app.services import idempotency, protocols, sites, visits
from app.services.errors import ServiceError


def _open_batch(db_path: str) -> dict:
    enabled = next(s for s in sites.list_sites(db_path) if s["status"] == "启用")
    active = protocols.active_protocol(db_path)
    return visits.create_batch(
        enabled["id"],
        protocol_id=active["id"] if active else None,
        note="幂等键败测批次",
        db_path=db_path,
    )


def _payload() -> str:
    return json.dumps(
        {"visit_date": "2026-11-20", "investigator": "测试医生", "weight_kg": 61},
        ensure_ascii=False,
    )


def _ledger_count(db_path: str) -> int:
    with get_db(db_path) as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM idempotency_ledger").fetchone()["c"]


def _row_count(db_path: str, batch_id: int) -> int:
    return visits.get_batch(batch_id, db_path)["item_count"]


# 败测 1：追加成功 → 账册必有落点行，戳记可重算核对
def test_append_writes_idempotency_landing_row(db_path):
    batch = _open_batch(db_path)
    before = _ledger_count(db_path)
    row = visits.add_visit_row(
        batch["id"], "IDM-0001", "V1", payload=_payload(),
        idem_key="idem:key-0001-aaaa", db_path=db_path,
    )
    assert _ledger_count(db_path) == before + 1
    with get_db(db_path) as conn:
        led = conn.execute(
            "SELECT * FROM idempotency_ledger WHERE row_id=?", (row["id"],)
        ).fetchone()
    assert led is not None
    assert dict(led)["status_flag"] == "现行"
    expected = idempotency.build_stamp(
        "visit-row-append", batch["id"], "IDM-0001", "V1", "idem:key-0001-aaaa"
    )
    assert dict(led)["stamp"] == expected
    assert dict(led)["idem_key"] == "idem:key-0001-aaaa"


# 败测 2：同键重复追加 → 不新增行、回放首笔、追加次数=2
def test_duplicate_idem_key_replays_without_new_row(db_path):
    batch = _open_batch(db_path)
    first = visits.add_visit_row(
        batch["id"], "IDM-0002", "V1", payload=_payload(),
        idem_key="idem:key-0002-bbbb", db_path=db_path,
    )
    rows_before = _row_count(db_path, batch["id"])
    replay = visits.add_visit_row(
        batch["id"], "IDM-0002", "V1", payload=_payload(),
        idem_key="idem:key-0002-bbbb", db_path=db_path,
    )
    assert replay["id"] == first["id"]
    assert replay["replay"] is True
    assert replay["attempt_count"] == 2
    assert _row_count(db_path, batch["id"]) == rows_before
    with get_db(db_path) as conn:
        led = conn.execute(
            "SELECT * FROM idempotency_ledger WHERE idem_key=?",
            ("idem:key-0002-bbbb",),
        ).fetchone()
    assert dict(led)["attempt_count"] == 2
    assert dict(led)["row_id"] == first["id"]


# 败测 3：两侧并发争用同键、均不显式加锁 → 恰好一行，不得双成功
def test_concurrent_same_key_only_one_wins(db_path):
    batch = _open_batch(db_path)
    barrier = threading.Barrier(2)
    outcomes: list[dict] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            barrier.wait()
            res = visits.add_visit_row(
                batch["id"], "IDM-0003", "V1", payload=_payload(),
                idem_key="idem:key-0003-cccc", db_path=db_path,
            )
            outcomes.append(res)
        except Exception as exc:  # noqa: BLE001 — 并发争用方允许失败
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert _row_count(db_path, batch["id"]) == 1
    with get_db(db_path) as conn:
        led_n = conn.execute(
            "SELECT COUNT(*) AS c FROM idempotency_ledger WHERE idem_key=?",
            ("idem:key-0003-cccc",),
        ).fetchone()["c"]
    assert led_n == 1
    # 成功的一次必须落行；另一侧要么回放同笔、要么收到争用/重复错误
    assert len(outcomes) + len(errors) == 2
    winner = [o for o in outcomes if not o.get("replay")]
    assert len(winner) == 1


# 败测 4：同键携带不同口径要素（戳记拧花）→ 409 拒绝
def test_stamp_mismatch_is_rejected(db_path):
    batch = _open_batch(db_path)
    visits.add_visit_row(
        batch["id"], "IDM-0004", "V1", payload=_payload(),
        idem_key="idem:key-0004-dddd", db_path=db_path,
    )
    rows_before = _row_count(db_path, batch["id"])
    with pytest.raises(ServiceError) as exc:
        visits.add_visit_row(
            batch["id"], "IDM-0099", "V2", payload=_payload(),
            idem_key="idem:key-0004-dddd", db_path=db_path,
        )
    assert exc.value.status_code == 409
    assert _row_count(db_path, batch["id"]) == rows_before


# 败测 5：整笔写库失败 → 账册与业务表双双无残留，错误讲明白
def test_write_failure_rolls_back_both_tracks(db_path, monkeypatch):
    batch = _open_batch(db_path)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("模拟访视行写库失败")

    monkeypatch.setattr(visits, "_append_visit_row_core", boom)
    with pytest.raises(ServiceError) as exc:
        visits.add_visit_row(
            batch["id"], "IDM-0005", "V1", payload=_payload(),
            idem_key="idem:key-0005-eeee", db_path=db_path,
        )
    monkeypatch.undo()
    assert "幂等" in exc.value.message or "写库" in exc.value.message
    with get_db(db_path) as conn:
        led = conn.execute(
            "SELECT COUNT(*) AS c FROM idempotency_ledger WHERE idem_key=?",
            ("idem:key-0005-eeee",),
        ).fetchone()["c"]
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM visit_rows WHERE batch_id=? AND subject_key='IDM-0005'",
            (batch["id"],),
        ).fetchone()["c"]
    assert led == 0
    assert rows == 0


# 败测 6：归档收口 → 新旗/只读副本/读侧分叉/再追加被拒
def test_archive_moves_expired_ledger_readonly(db_path):
    batch = _open_batch(db_path)
    visits.add_visit_row(
        batch["id"], "IDM-0006", "V1", payload=_payload(),
        idem_key="idem:key-0006-ffff", db_path=db_path,
    )
    visits.ingest_ok_rows(batch["id"], db_path=db_path)
    closed = visits.close_batch(batch["id"], db_path=db_path)
    assert closed["status"] == "已关闭"

    result = idempotency.archive_closed_batches(db_path)
    assert result["archived"] >= 1

    queue = idempotency.list_ledger(status_flag="现行", db_path=db_path)
    assert all(item["batch_id"] != batch["id"] for item in queue)
    archived = idempotency.list_archive(batch_id=batch["id"], db_path=db_path)
    assert any(item["idem_key"] == "idem:key-0006-ffff" for item in archived)
    with get_db(db_path) as conn:
        flag = conn.execute(
            "SELECT status_flag FROM idempotency_ledger WHERE idem_key=?",
            ("idem:key-0006-ffff",),
        ).fetchone()["status_flag"]
    assert flag == "已归档"

    with pytest.raises(ServiceError):
        visits.add_visit_row(
            batch["id"], "IDM-0007", "V1", payload=_payload(),
            idem_key="idem:key-0007-gggg", db_path=db_path,
        )


# 败测 7：批次列表带幂等徽标与追加次数，且与账册一致
def test_batch_list_carries_idempotency_badge(db_path):
    batch = _open_batch(db_path)
    visits.add_visit_row(
        batch["id"], "IDM-0008", "V1", payload=_payload(),
        idem_key="idem:key-0008-hhhh", db_path=db_path,
    )
    visits.add_visit_row(
        batch["id"], "IDM-0008", "V1", payload=_payload(),
        idem_key="idem:key-0008-hhhh", db_path=db_path,
    )
    listed = next(b for b in visits.list_batches(db_path) if b["id"] == batch["id"])
    assert listed["idem_riveted"] is True
    assert listed["idem_keys"] == 1
    assert listed["append_attempts"] == 2
    detail = visits.get_batch(batch["id"], db_path)
    assert detail["idem_summary"]["keys"] == 1
    assert detail["idem_summary"]["attempts"] == 2
    assert detail["items"][0]["idem_key"] == "idem:key-0008-hhhh"


# 败测 8：非法请求键拒绝；留空时按口径生成确定性键
def test_key_format_validation_and_generation(db_path):
    batch = _open_batch(db_path)
    with pytest.raises(ServiceError):
        visits.add_visit_row(
            batch["id"], "IDM-0010", "V1", payload=_payload(),
            idem_key="bad key!!", db_path=db_path,
        )
    auto = idempotency.build_business_key(batch["id"], "IDM-0011", "V1")
    assert auto == f"{batch['id']}:IDM-0011:V1"
    row_a = visits.add_visit_row(
        batch["id"], "IDM-0011", "V1", payload=_payload(), db_path=db_path
    )
    row_b = visits.add_visit_row(
        batch["id"], "IDM-0011", "V1", payload=_payload(), db_path=db_path
    )
    assert row_b["id"] == row_a["id"]
    assert row_b["replay"] is True


# 败测 9：服务端自动键第二次提交同样命中重放（与 8 合并口径，显式断言）
def test_auto_key_replay_is_idempotent(db_path):
    batch = _open_batch(db_path)
    first = visits.add_visit_row(
        batch["id"], "IDM-0012", "V2", payload=_payload(), db_path=db_path
    )
    again = visits.add_visit_row(
        batch["id"], "IDM-0012", "V2", payload=_payload(), db_path=db_path
    )
    assert again["id"] == first["id"]
    assert again["idem_key"] == idempotency.build_business_key(batch["id"], "IDM-0012", "V2")


# 败测 10：台参数暴露且仅暴露幂等键口径项
def test_settings_expose_idempotency_spec(db_path):
    from app.services import settings as settings_svc

    item = settings_svc.get_setting("idempotency_key_spec", db_path=db_path)
    assert "口径" in item["label"] or "幂等" in item["label"]
    snapshot = settings_svc.bulk_snapshot(db_path)
    assert "idempotency_key_spec" in snapshot
    spec = snapshot["idempotency_key_spec"]
    assert "sha256" in spec and "现行" in spec and "已归档" in spec
