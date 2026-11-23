"""端到端验证：真实 CherryPy 应用 + 真实业务库（非无头浏览器）。

覆盖：登录 → 批次列表徽标/追加次数 → 显式幂等键追加 → 同键重放挡回刷 →
戳记拧花 409 → 非法键 409 → 账册队列 → 关闭批次 → 归档收口 → 归档只读。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmp = tempfile.mkdtemp(prefix="idem-e2e-")
os.environ["DATABASE_PATH"] = os.path.join(tmp, "e2e.db")
os.environ["INSTANCE_DIR"] = tmp

import cherrypy  # noqa: E402
import httpx  # noqa: E402

from app.main import create_app  # noqa: E402
from app.services import sites, visits  # noqa: E402

create_app()
transport = httpx.WSGITransport(app=cherrypy.tree)
client = httpx.Client(transport=transport, base_url="http://testserver")

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS" if cond else "FAIL"), "-", name, detail)
    if not cond:
        failures.append(name)


# 1. 登录
r = client.post("/api/login", json={"username": "admin", "password": "123456"})
check("登录成功", r.status_code == 200 and r.json().get("ok"))

# 2. 选一个启用中心开批次
site = next(s for s in sites.list_sites() if s["status"] == "启用")
r = client.post("/api/batches", json={"site_id": site["id"], "note": "幂等E2E"})
check("开批次", r.status_code == 200)
bid = r.json()["id"]

# 3. 显式幂等键追加
idem_key = "e2e:idem-0001-xyz"
payload = json.dumps(
    {"visit_date": "2026-11-20", "investigator": "E2E医生", "weight_kg": 70},
    ensure_ascii=False,
)
r = client.post(
    f"/api/batches/{bid}/items",
    json={"subject_key": "E2E-001", "visit_code": "V1", "payload": payload,
          "idem_key": idem_key},
)
check("首笔追加成功", r.status_code == 200 and r.json().get("replay") is False)
row_id = r.json()["id"]

# 4. 同键重放：不新增行
r2 = client.post(
    f"/api/batches/{bid}/items",
    headers={"Idempotency-Key": idem_key},
    json={"subject_key": "E2E-001", "visit_code": "V1", "payload": payload,
          "idem_key": idem_key},
)
check("同键重放挡回刷",
      r2.status_code == 200 and r2.json().get("replay") is True
      and r2.json()["id"] == row_id and r2.json()["attempt_count"] == 2,
      str(r2.json()))

# 5. 批次列表徽标 + 追加次数
r = client.get("/api/batches")
b = next(x for x in r.json()["items"] if x["id"] == bid)
check("列表幂等徽标亮起", b["idem_riveted"] is True and b["idem_keys"] == 1)
check("列表追加次数=2", b["append_attempts"] == 2, str(b.get("append_attempts")))

# 6. 详情落点行带幂等键 + 摘要台
r = client.get(f"/api/batches/{bid}")
d = r.json()
check("详情幂等摘要", d["idem_summary"]["keys"] == 1 and d["idem_summary"]["attempts"] == 2)
check("落点行带幂等键", any(i.get("idem_key") == idem_key for i in d["items"]))

# 7. 戳记拧花：同键不同口径 → 409
r = client.post(
    f"/api/batches/{bid}/items",
    json={"subject_key": "E2E-999", "visit_code": "V2", "payload": payload,
          "idem_key": idem_key},
)
check("戳记拧花拦截409", r.status_code == 409, str(r.status_code))

# 8. 非法幂等键 → 409
r = client.post(
    f"/api/batches/{bid}/items",
    json={"subject_key": "E2E-002", "visit_code": "V1", "payload": payload,
          "idem_key": "bad key!!"},
)
check("非法幂等键拦截", r.status_code == 409, str(r.status_code))

# 9. 账册队列页签
r = client.get(f"/api/idempotency/ledger?batch_id={bid}&status_flag=现行")
led = r.json()["items"]
check("账册队列有落点", len(led) == 1 and led[0]["idem_key"] == idem_key
      and led[0]["row_id"] == row_id)

# 10. 自动键（不传 idem_key）也幂等
r = client.post(f"/api/batches/{bid}/items",
                json={"subject_key": "E2E-010", "visit_code": "V3", "payload": payload})
auto_id = r.json()["id"]
r = client.post(f"/api/batches/{bid}/items",
                json={"subject_key": "E2E-010", "visit_code": "V3", "payload": payload})
check("自动键重放", r.json().get("replay") is True and r.json()["id"] == auto_id)

# 11. 台参数口径
r = client.get("/api/idempotency/spec")
spec = r.json()
check("台参数只展幂等口径", spec["key"] == "idempotency_key_spec"
      and "sha256" in spec["value"] and "现行" in spec["value"])
r = client.post("/api/settings/update",
                json={"key": "idempotency_key_spec", "value": "x"})
check("口径项只读不可改", r.status_code == 400, str(r.status_code))

# 12. 关闭批次 → 归档收口（乙路）
client.post(f"/api/batches/{bid}/ingest", json={})
r = client.post(f"/api/batches/{bid}/close", json={})
check("关闭批次", r.status_code == 200 and r.json()["status"] == "已关闭",
      str(r.json()) if r.status_code != 200 else "")
r = client.post("/api/idempotency/seal", json={})
check("归档收口执行", r.status_code == 200 and r.json()["archived"] >= 1,
      str(r.json()))

# 13. 读侧分叉：现行队列读不到、归档表有只读副本
r = client.get(f"/api/idempotency/ledger?batch_id={bid}&status_flag=现行")
check("现行队列已清空", r.json()["items"] == [])
r = client.get(f"/api/idempotency/archive?batch_id={bid}")
arc = r.json()["items"]
check("归档表有只读副本", any(x["idem_key"] == idem_key for x in arc))

# 14. 归档后同键再追加被拒
r = client.post(
    f"/api/batches/{bid}/items",
    json={"subject_key": "E2E-001", "visit_code": "V1", "payload": payload,
          "idem_key": idem_key},
)
check("归档后再追加被拒", r.status_code in (400, 409), str(r.status_code))

# 15. 业务库直查：行数与账册一致，无重复灌行
import sqlite3  # noqa: E402
conn = sqlite3.connect(os.environ["DATABASE_PATH"])
n_rows = conn.execute(
    "SELECT COUNT(*) FROM visit_rows WHERE batch_id=?", (bid,)
).fetchone()[0]
n_led = conn.execute(
    "SELECT COUNT(*) FROM idempotency_ledger WHERE batch_id=?", (bid,)
).fetchone()[0]
n_arc = conn.execute(
    "SELECT COUNT(*) FROM idempotency_archive WHERE batch_id=?", (bid,)
).fetchone()[0]
check("业务库行数=落点键数（无虚高）", n_rows == n_led, f"rows={n_rows} ledger={n_led}")
check("归档表档数=账册档数", n_arc == n_led, f"archive={n_arc} ledger={n_led}")
conn.close()

print("\n数据库文件：", os.environ["DATABASE_PATH"])
print("失败项：", failures or "无")
raise SystemExit(1 if failures else 0)
