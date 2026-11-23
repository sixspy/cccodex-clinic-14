"""幂等键服务：访视批次追加抓取的旁路账册。

甲路（已选边，端到端接线）
    追加访视行前在 ``idempotency_ledger`` 当场铆死幂等键（UNIQUE +
    BEGIN IMMEDIATE 写锁），并钤印 sha256 戳记；同键重放挡回刷，
    戳记拧花拒绝，整笔失败同事务回滚。

乙路（甲边封存后的收口段，不另立并行读轨）
    关联批次已关闭的过期档迁入只读 ``idempotency_archive``，账册原档
    置 ``已归档`` 新旗；现行读轨只认 ``现行`` 旗。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from app.db import get_db
from app.services import audit as audit_svc
from app.services.errors import ServiceError

SCOPE_ROW_APPEND = "visit-row-append"
FLAG_ACTIVE = "现行"
FLAG_ARCHIVED = "已归档"
KEY_RE = re.compile(r"^[A-Za-z0-9:_-]{8,128}$")
IDEM_CONFLICT = 409


def build_business_key(batch_id: int, subject_key: str, visit_code: str) -> str:
    """业务幂等键口径：批次ID:受试者键:访视编码。"""
    return f"{int(batch_id)}:{subject_key}:{visit_code}"


def build_stamp(
    scope: str, batch_id: int, subject_key: str, visit_code: str, idem_key: str
) -> str:
    """钤印：sha256(scope|batch|subject|visit|idem_key)。"""
    raw = "|".join(
        [scope, str(int(batch_id)), subject_key, visit_code, idem_key]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_request_key(idem_key: str | None) -> str:
    """校验客户端显式提交的请求幂等键；非法即拒。"""
    key = (idem_key or "").strip()
    if key and not KEY_RE.fullmatch(key):
        raise ServiceError(
            "幂等键格式无效：仅允许 8-128 位字母数字及 :_-", IDEM_CONFLICT
        )
    return key


def backfill_legacy(db_path: str | None = None) -> int:
    """为历史访视行按业务口径补植账册落点，仅补缺、不覆盖。"""
    planted = 0
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT r.id AS row_id, r.batch_id, r.subject_key, r.visit_code
            FROM visit_rows r
            WHERE NOT EXISTS (
              SELECT 1 FROM idempotency_ledger l WHERE l.row_id=r.id
            )
            ORDER BY r.id
            """
        ).fetchall()
        for r in rows:
            key = build_business_key(r["batch_id"], r["subject_key"], r["visit_code"])
            exists = conn.execute(
                "SELECT 1 FROM idempotency_ledger WHERE idem_key=?", (key,)
            ).fetchone()
            if exists:
                continue
            stamp = build_stamp(
                SCOPE_ROW_APPEND,
                r["batch_id"], r["subject_key"], r["visit_code"], key,
            )
            conn.execute(
                """
                INSERT INTO idempotency_ledger(
                  idem_key, scope, batch_id, subject_key, visit_code,
                  row_id, stamp, status_flag, attempt_count, last_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, '')
                """,
                (
                    key, SCOPE_ROW_APPEND, r["batch_id"],
                    r["subject_key"], r["visit_code"], r["row_id"], stamp,
                    FLAG_ACTIVE,
                ),
            )
            planted += 1
    return planted


def append_visit_row_idempotent(
    batch_id: int,
    subject_key: str,
    visit_code: str,
    payload: str = "",
    quality: str | None = None,
    idem_key: str | None = None,
    db_path: str | None = None,
) -> dict:
    """甲路核心：当场铆键 + 钤印 + 业务写库，单事务提交即刻落定。"""
    # 延迟导入避免与 visits 模块循环依赖
    from app.services import visits as visits_svc

    request_key = validate_request_key(idem_key)
    subject_key = (subject_key or "").strip()
    visit_code = (visit_code or "").strip().upper()
    if not request_key:
        request_key = build_business_key(batch_id, subject_key, visit_code)
    stamp = build_stamp(
        SCOPE_ROW_APPEND, batch_id, subject_key, visit_code, request_key
    )

    conn = sqlite3.connect(_resolve_path(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        # 写锁当场铆死，两侧争用在此串行化，杜绝双成功
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM idempotency_ledger WHERE idem_key=?", (request_key,)
        ).fetchone()
        if existing:
            led = dict(existing)
            if led["stamp"] != stamp:
                raise ServiceError(
                    "幂等键戳记拧花：同键提交口径与首笔不一致，已拦截重复追加",
                    IDEM_CONFLICT,
                )
            if led["status_flag"] == FLAG_ARCHIVED:
                raise ServiceError(
                    "该幂等键已归档收口，不能再追加", IDEM_CONFLICT
                )
            conn.execute(
                "UPDATE idempotency_ledger SET attempt_count=attempt_count+1, "
                "updated_at=datetime('now') WHERE id=?",
                (led["id"],),
            )
            row = conn.execute(
                "SELECT * FROM visit_rows WHERE id=?", (led["row_id"],)
            ).fetchone()
            data = dict(row) if row else {}
            data["replay"] = True
            data["attempt_count"] = led["attempt_count"] + 1
            data["idem_key"] = request_key
            conn.commit()
            return data

        # 业务校验与写库在同一事务内；失败整笔回滚，账册不留残
        try:
            row = visits_svc._append_visit_row_core(
                conn, batch_id, subject_key, visit_code, payload, quality
            )
        except ServiceError:
            conn.rollback()
            raise
        conn.execute(
            """
            INSERT INTO idempotency_ledger(
              idem_key, scope, batch_id, subject_key, visit_code,
              row_id, stamp, status_flag, attempt_count, last_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                request_key, SCOPE_ROW_APPEND, batch_id, subject_key,
                visit_code, row["id"], stamp, FLAG_ACTIVE,
                json.dumps({"row_id": row["id"], "quality": row["quality"]},
                           ensure_ascii=False),
            ),
        )
        conn.commit()
    except ServiceError:
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        # UNIQUE 争用落败方：读首笔结果回放，绝不双写
        landed = conn.execute(
            "SELECT * FROM idempotency_ledger WHERE idem_key=?", (request_key,)
        ).fetchone()
        if landed and dict(landed)["stamp"] == stamp:
            row = conn.execute(
                "SELECT * FROM visit_rows WHERE id=?",
                (dict(landed)["row_id"],),
            ).fetchone()
            data = dict(row) if row else {}
            data["replay"] = True
            data["attempt_count"] = dict(landed)["attempt_count"]
            data["idem_key"] = request_key
            return data
        raise ServiceError(f"幂等键争用写库失败：{exc}", IDEM_CONFLICT) from exc
    except sqlite3.Error as exc:
        conn.rollback()
        raise ServiceError(f"幂等键整笔写库失败，已回滚：{exc}", 500) from exc
    finally:
        conn.close()

    result = dict(row)
    result["replay"] = False
    result["attempt_count"] = 1
    result["idem_key"] = request_key
    audit_svc.write_event(
        "幂等铆键追加访视行",
        "idempotency_ledger",
        row["id"],
        f"{request_key} -> row={row['id']}",
        db_path=db_path,
    )
    return result


def list_ledger(
    db_path: str | None = None,
    batch_id: int | None = None,
    status_flag: str | None = FLAG_ACTIVE,
) -> list[dict]:
    backfill_legacy(db_path)
    clauses = []
    params: list = []
    if batch_id:
        clauses.append("batch_id=?")
        params.append(int(batch_id))
    if status_flag:
        clauses.append("status_flag=?")
        params.append(status_flag)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT l.*, b.batch_code, s.name AS site_name
            FROM idempotency_ledger l
            JOIN visit_batches b ON b.id=l.batch_id
            JOIN trial_sites s ON s.id=b.site_id
            {where}
            ORDER BY l.id DESC
            LIMIT 500
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def list_archive(
    db_path: str | None = None,
    batch_id: int | None = None,
) -> list[dict]:
    clauses = []
    params: list = []
    if batch_id:
        clauses.append("a.batch_id=?")
        params.append(int(batch_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT a.*, b.batch_code
            FROM idempotency_archive a
            JOIN visit_batches b ON b.id=a.batch_id
            {where}
            ORDER BY a.id DESC
            LIMIT 500
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def summary_for_batch(batch_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        agg = conn.execute(
            """
            SELECT COUNT(*) AS keys,
                   COALESCE(SUM(attempt_count), 0) AS attempts,
                   SUM(CASE WHEN status_flag='已归档' THEN 1 ELSE 0 END) AS archived
            FROM idempotency_ledger
            WHERE batch_id=?
            """,
            (batch_id,),
        ).fetchone()
        return {
            "keys": agg["keys"],
            "attempts": agg["attempts"],
            "archived": agg["archived"] or 0,
            "riveted": agg["keys"] > 0,
        }


def archive_closed_batches(db_path: str | None = None) -> dict:
    """乙路收口：已关闭批次的现行档迁入只读归档并换新旗。"""
    archived = 0
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        targets = conn.execute(
            """
            SELECT l.* FROM idempotency_ledger l
            JOIN visit_batches b ON b.id=l.batch_id
            WHERE l.status_flag=? AND b.status='已关闭'
            """,
            (FLAG_ACTIVE,),
        ).fetchall()
        for l in targets:
            conn.execute(
                """
                INSERT OR IGNORE INTO idempotency_archive(
                  ledger_id, idem_key, scope, batch_id, subject_key,
                  visit_code, row_id, stamp, attempt_count, last_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    l["id"], l["idem_key"], l["scope"], l["batch_id"],
                    l["subject_key"], l["visit_code"], l["row_id"],
                    l["stamp"], l["attempt_count"], l["last_result"],
                ),
            )
            conn.execute(
                "UPDATE idempotency_ledger SET status_flag=?, "
                "updated_at=datetime('now') WHERE id=?",
                (FLAG_ARCHIVED, l["id"]),
            )
            archived += 1
        conn.commit()
    if archived:
        audit_svc.write_event(
            "幂等账册归档收口",
            "idempotency_archive",
            0,
            f"归档{archived}档",
            db_path=db_path,
        )
    return {"archived": archived}


def _resolve_path(db_path: str | None) -> str:
    from app.config import Config

    return str(db_path) if db_path else str(Config.DATABASE_PATH)
