# 复现纸 · backfill-bug-03 相交挂窗 / 叠日重放修复

> 现象：新开窗与在册窗日界相交却照样挂上；重放叠日进同一落点；页面勾选（落库策略）形同虚设。
> 修复范围：冲突队列、日界详情、相交卡片、回灌水线四处联查。

## 1. 缺陷定位（修复前）

| # | 位置 | 缺陷 |
|---|------|------|
| 1 | `app/services/overlap.py` | 种子 `overlap_check='ui_only'`，该模式下相交窗静默插库，还写 `conflict_queue(accepted=1)`；前端没有任何真实开关 |
| 2 | `app/db.py` | `window_registry` 无区间排他约束，挂窗无写锁 → 左右调度同时点挂窗双写 |
| 3 | `app/db.py` + 前端 | `day_bound_live/gate` 是两条写死 meta，页面硬编码“双界未对齐” → 双界不同源 |
| 4 | `app/services/windows.py:replay_slice` | 跨窗同一切片日无互斥 → 叠日重放同一落点 |
| 5 | `static/js/app.js` | 挂窗请求写死 `{}`，无表单、无拒登芯片、四面板不联动 |

## 2. 修复设计

### 2.1 相交拦截二选一落库（页面开关真实生效，默认 `hard_unique`）

- **`hard_unique`（唯一约束拦截）**：`BEGIN IMMEDIATE` 写事务内应用层判定 + SQLite 触发器
  `trg_window_no_overlap_insert/update` 硬兜底；相交即回短句 `日界相交，拒登`，HTTP 409，**库存不留相交窗**，冲突队列不记账。
- **`conflict_ledger`（旁路冲突账册）**：先用独立短事务往 `conflict_queue(rejected=1, accepted=0)` 记一笔拒登，
  再开写事务权威判定；相交则不插窗行、整笔撤回开窗。**选边差异：账册留痕，库存仍不留相交窗。**
- 历史值 `ui_only` 在 `init_db` 迁移与 `get_mode` 读取处统一归一为 `hard_unique`，杜绝“只改提示不改写库”。

### 2.2 双界同源，双界即拦

- 现行日界 `live = MAX(window_registry.end_date)`；可挂门槛 `gate = live + 1 天`，同一函数 `_day_bounds` 派生，`aligned=true`。
- 挂窗权威判定含门槛：`start <= live` 即拦（擦边共日按相交处理：`a.start <= b.end AND b.start <= a.end`）。

### 2.3 并发防双写

- 新增 `db.write_tx()`：`isolation_level=None` + `BEGIN IMMEDIATE`，挂窗即抢库级写锁，并发请求串行化；
  触发器作为最后防线，后写者收 `IntegrityError('日界相交，拒登')` 转白话 409。

### 2.4 重放叠日互斥

- 应用层预检：同一 `slice_date` 已有别的窗口 `已落库` → 409 `该日已有窗口落库，禁止叠日重放`。
- 触发器 `trg_slice_no_overlap_loaded` 兜底；水线新增 `slice_overlap_rows` 指标，切片重跑与水线再验均不得出现叠日行。

### 2.5 前端联动（原生 JS）

- 冲突队列面板：挂窗表单（目标表/起止日/落库策略下拉），切换下拉即调 `/api/overlap/mode` 持久化；
  拒登时页面点亮红色「拒登」芯片，队列卡片同亮拒登标记。
- 相交卡片：同源双界 + “双界已对齐”；回灌水线：库存叠日行 + 切片叠日行，任一非零亮拒登芯片。

## 3. 复现步骤

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # 若 uvicorn[standard] 解析失败，改装 uvicorn（无 extras）
.venv/bin/python -m pytest tests/ -q               # 14 passed

# 端到端
DATABASE_PATH=/tmp/bf/lab.db PORT=8070 .venv/bin/python run.py &
curl -c cj.txt -X POST http://127.0.0.1:8070/api/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"123456"}'

# ① 再试一次相交挂窗 → 409，chip=拒登（种子库存 11-10~11-12）
curl -b cj.txt -X POST http://127.0.0.1:8070/api/overlap/hang \
  -H 'Content-Type: application/json' \
  -d '{"table_name":"dw_x","start_date":"2026-11-11","end_date":"2026-11-13"}'

# ② 拒登后再试擦边日界（开始日=库存结束日 11-12）→ 仍 409 拒登
curl -b cj.txt -X POST http://127.0.0.1:8070/api/overlap/hang \
  -H 'Content-Type: application/json' \
  -d '{"table_name":"dw_edge","start_date":"2026-11-12","end_date":"2026-11-12"}'

# ③ 相邻不共日（11-13 起）→ 200 已挂，双界推进到 live=11-14 / gate=11-15
curl -b cj.txt -X POST http://127.0.0.1:8070/api/overlap/hang \
  -H 'Content-Type: application/json' \
  -d '{"table_name":"dw_next","start_date":"2026-11-13","end_date":"2026-11-14"}'

# ④ 切账册选边后再相交 → 409 拒登，conflict_queue 留 rejected=1，库存窗行撤回
curl -b cj.txt -X POST http://127.0.0.1:8070/api/overlap/mode \
  -H 'Content-Type: application/json' -d '{"mode":"conflict_ledger"}'
curl -b cj.txt -X POST http://127.0.0.1:8070/api/overlap/hang \
  -H 'Content-Type: application/json' \
  -d '{"table_name":"dw_led","start_date":"2026-11-11","end_date":"2026-11-12"}'

# ⑤ 相交卡片与回灌水线同屏核对
curl -b cj.txt http://127.0.0.1:8070/api/overlap/panels
#   card.aligned=true；waterline.overlap_rows=0、slice_overlap_rows=0

# ⑥ 本地库核查：库存无双窗同日，触发器在位
sqlite3 /tmp/bf/lab.db \
 "SELECT id,table_name,start_date,end_date FROM window_registry;
  SELECT name FROM sqlite_master WHERE type='trigger';"
```

## 4. 验收对照

| 验收要求 | 结果 |
|---|---|
| 再试相交挂窗，拒登芯片须亮 | 409 + `chip:"拒登"`，前端红色芯片与队列卡片同亮 |
| 水线日界不得出现叠日行 | `overlap_rows=0`、`slice_overlap_rows=0` |
| 拒登后擦边日界同屏核对，库存无双窗同日 | 擦边 409；`window_registry` 任意两行区间不相交（含共日） |
| 相交拦截二选一，选边改变相交窗是否留库存 | 硬约束不留痕；账册留 `rejected=1` 记录；两模式库存均不留相交窗 |
| 双界同源、双界即拦 | `_day_bounds` 同源派生；`start<=live` 即拦 |
| 左右调度同时挂窗不双写 | `BEGIN IMMEDIATE` + 触发器，并发测试一成一拒 |
| 切片重跑与水线再验不得叠日 | 跨窗同日重放 409 白话；同片重跑 409；水线恒 0 |
| 不只改勾选提示，窗界写库同改 | 触发器为库级硬约束，绕过应用层裸插同样被拒（`test_trigger_blocks_raw_insert`） |

## 5. 测试清单（14 项全绿）

- 基线：登录页/登录/计划-重放-对账主链（3）
- 相交：硬约束拒登、擦边共日、相邻放行、账册选边、模式归一、双界门槛、并发无双写、裸插触发器、叠日重放、API 拒登芯片、非法日期白话（11）
