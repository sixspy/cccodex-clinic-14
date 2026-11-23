# cc-clinic-lab · 临床试验访视采集台

数据工程产品线基线。题号前缀 `clinic-*`，默认端口 **7600**。

## 技术栈

- 后端：CherryPy + SQLite
- 前端：原生 JS SPA（顶栏薄荷绿丝带导航）
- 会话：itsdangerous 签名 Cookie
- 测试：pytest + httpx（依赖已列入 requirements）

## 登录

- 地址：`http://127.0.0.1:7600/login`
- 账号：`admin` / `123456`

## 页面

| 页面 | 能力 |
|------|------|
| 试验中心 | 中心登记、启停/观察、配额、方案版本生效 |
| 访视批次 | 开批、模拟抓取、追加访视行（幂等键铆死/钤印/挡回刷）、合格入库、收齐/关闭 |
| 字段质控 | 按批/全量扫描、发现关闭/重开 |
| 缺口补采 | 排队、执行成功/失败、失败重试 |
| 访视对账 | 缺口/发现/补采对照、中心矩阵、审计 |
| 台参数 | 设置读写、幂等键口径（只读） |

## 幂等键（clinic-14）

访视批次追加抓取以幂等键兜底，防止同批重复灌行导致质控/补采数字虚高。

- **甲路（已选边）旁路账册 `idempotency_ledger`**：追加前 `BEGIN IMMEDIATE`
  写锁 + 幂等键 `UNIQUE` 当场铆死，并钤印
  `sha256(scope|batch|subject|visit|idem_key)`；同键重放只回放首笔结果、
  不新增行（`replay=true`、`attempt_count+1`），戳记拧花返回 409，
  整笔写库失败同事务回滚。读轨只认本账册现行旗，不回刷业务表。
- **乙路只读归档 `idempotency_archive`**：批次关闭后由「归档收口」把过期档
  迁入只读归档表并置 `已归档` 旗；现行队列读不到，归档后同键再追加被拒。
- 键口径：业务键 `批次ID:受试者键:访视编码`（未显式提交时服务端确定性生成）；
  显式请求键 `Idempotency-Key` 限 `^[A-Za-z0-9:_-]{8,128}$`。
- 批次列表亮幂等徽标并展示追加次数（含重放×N），详情给幂等摘要台与每行落点。
- 台参数仅展示只读口径项 `idempotency_key_spec`，不可改写。
- 设计与败测清单见 `docs/幂等键设计.md`，败测见 `tests/test_idempotency.py`。

## 主链

中心 → 访视批次抓取 → 质控发现 → 缺口补采 → 对账报告；设置与审计贯穿写操作。

## 本地

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

Docker：`docker compose up -d --build`，健康检查打 `/login`。

## 后续题面预留切口

- 访视行跨中心去重与冲突裁决
- 方案换版后存量批次字段兼容扫描
- 补采分片与并发上限
- 对账差异工单与导出
