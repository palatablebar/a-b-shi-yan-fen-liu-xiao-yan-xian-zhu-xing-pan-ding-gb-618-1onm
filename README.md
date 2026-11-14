# A/B 实验分流校验与显著性判定（`abcheck`）

> 数据工程 · 数据应用 · 中型多模块系统。原生 HTML/CSS/JS 前端 + Python FastAPI 后端 + Celery worker + MySQL 8.0 + Redis，5 个容器一键起。

## 1. 场景

- **模拟客户**：橙野 App · 增长实验组（7 人），每周 8 个实验，1 个 App × 4 个渠道（信息流 / 推送 / 搜索 / 小程序）。
- **现有工作流**：实验平台只负责分流，结果导出 CSV 丢给分析师跑 SQL + Excel，
  卡方和 t 检验各算一遍，p 值 < 0.05 就写进上线邮件。
- **三个痛点**：
  1. **分流不均没查（SRM）**：某 SDK 版本把 50/50 分成了 52.3/47.7，对照组凭空多 138 个样本，指标差异全被污染；
  2. **只看 p 值不看功效与最小可检测效应**：5,000 份样本去检测 1.1pp 的效应，功效只有 45%，算出来的「显著」其实是噪音；
  3. **多指标同时看没做多重比较校正**：一次实验 4 个指标，随便哪个 p<0.05 就宣布成功，假发现率失控。
- **上下游**：埋点 → 实验平台（分流）→ **本系统（分流校验 → 指标检验 → 决策建议）** → 产品决策会 / 上线评审。
- **分析版本（本系统的口径边界）**：一次分析 = 负责人登记的**一组可用曝光区间** + 按这组区间整套算出的
  **分流校验 / 全部指标 / 建议**。埋点故障、暂停、补数的日期不登记进区间，因此不会被整段累计；
  区间一调整，旧版本立即失效，指向旧版本的决策会被拒绝。详见第 10 节「分析版本」。
- **一天的工作剧本**：
  1. 09:20 实验负责人 `admin@abcheck.io` 登录，总览上 `EXP-2603-CARTBTN` 挂着 **SRM 告警**（χ²=12.696, p=0.000366）；
  2. 10:00 打开「样本与分流」，先为本次分析登记**可用曝光区间**（如 `2026-06-01~2026-06-09` +
     `2026-06-12~2026-06-15`，跳过埋点故障的两天），点「登记区间并整套发布」；页面显示
     control 3,138 / treat_short 2,862（计划 50/50，实测 52.3% / 47.7%），每变体所需样本 11,780，
     当前功效只有 28.1% —— 先去查分流 SDK；
  3. 11:00 打开 `EXP-2610-CASH8`，主指标下单转化率 10.09% → 11.19%，lift +10.89%，Wald CI [+0.25pp, +1.94pp]；
  4. 11:10 切到「指标检验」跑 BH 校正：4 个指标里主指标 q=0.0436 < 0.05，另外 3 个不显著；
     森林图上只有主指标的点越过 0 之外；
  5. 11:30 出决策「上线」，决策绑定当前分析版本（v2，区间 06-01~06-09 + 06-12~06-15），
     理由自动带上 lift / q / 样本量，写进 06-22 决策会纪要；
  6. 14:00 分析师 `analyst@abcheck.io` 导出 `metrics.csv`（28 条检验结果，带版本列）做周报，
     审计日志记下这次导出与导出的分析版本；
  7. 17:30 业务只读 `viewer@abcheck.io` 打开总览和实验台账，导出按钮不存在；直接请求导出接口真实返回 **403**。

## 2. 角色与权限

| 角色 | 账号 | 权限 | 典型动作 |
|---|---|---|---|
| 实验负责人 `admin` | `admin@abcheck.io` | 读写实验、录分流样本、登记可用曝光区间并整套发布分析、出决策、导出、看审计 | 建实验、登记区间、写决策 |
| 数据分析师 `analyst` | `analyst@abcheck.io` | 读实验/样本/分析版本/检验/决策 + 导出 CSV | 导出 `metrics.csv` 做周报（带版本列） |
| 业务只读 `viewer` | `viewer@abcheck.io` | 只读列表与详情 | 看总览、翻实验台账与版本 |

密码统一 `Demo@2026`。跨权调用返回真实 HTTP 状态：无 token **401**，viewer 写/发布分析/导出/审计 **403**，
非法参数（含非法/重叠的曝光区间）**422**，重复自然键（实验编号 / 分流样本格 / 同一版本的重复决策）**409**；
对失效版本的决策、无生效版本时的决策同样返回 **409**。

## 3. 业务模块表（6 个）

| # | 业务模块 | 对应页面 | 后端 services / controller | 主要 API |
|---|---|---|---|---|
| ① | 实验登记（实验 + 变体） | `#/experiments` 实验登记 | `experiment_service` / `variant_service` · `experiment_controller` | `GET/POST /api/experiments`、`GET/PATCH /api/experiments/{id}` |
| ② | 分流样本接入（变体×渠道×日期×指标） | `#/assignments` 样本与分流 | `assignment_service` · `assignment_controller` | `GET /api/experiments/{id}/samples`、`GET/POST /api/experiments/{id}/assignments` |
| ③ | 可用曝光区间与分析版本（横切口径） | `#/assignments`（分析版本面板）+ `#/metrics`（版本选择） | `analysis_service` + `algorithms/exposure.py` · `analysis_controller` | `POST /api/experiments/{id}/analysis-runs`、`GET /api/experiments/{id}/analysis-versions` |
| ④ | SRM 与功效校验（算法 A，版本内） | `#/assignments`（SRM 卡）+ `#/overview`（告警） | `algorithms/srm.py` + `stats.py` · `srm_service` · `metric_controller` | `GET /api/experiments/{id}/srm`（读生效版本快照） |
| ⑤ | 指标检验（算法 B，版本内） | `#/metrics` 指标检验 | `algorithms/metric_test.py` · `metric_service` · `metric_controller` | `GET /api/metrics`（默认仅生效版本）、`GET /api/experiments/{id}/analysis/{version_id}/metrics` |
| ⑥ | 多重比较与决策（绑定版本） | `#/metrics`（q 值对比）+ `#/decisions` | `metric_service` / `decision_service` · `decision_controller` | `GET /api/decisions`、`POST /api/experiments/{id}/decisions` |
| ⑦ | 报告导出与台账总览（可追溯版本） | `#/decisions`（导出）+ `#/overview` | `export_service` / `overview_service` · `export_controller` | `GET /api/exports/{experiments,assignments,metrics,decisions}.csv?experiment_id=&analysis_version_id=`、`GET /api/overview` |

横切能力（不算业务模块）：登录/JWT、角色鉴权、审计日志、限流、worker 心跳。

## 4. 快速启动

```bash
cp .env.example .env
docker compose up -d --build   # 等 5/5 healthy（实测约 40s）
open http://localhost:18207
```

首次启动 backend 自动建表并跑一次 seed（`SEED_DEMO=true`）。seed 幂等，可重复执行：

```bash
docker compose exec -T backend python -m scripts.seed_demo
# SEED_SUMMARY brand=abcheck users=3 sources=6 runs=1092 findings=21 pending=2
# seed 幂等：6 个实验各登记一段 7 天可用曝光区间，重复执行不新增分析版本
```

## 5. 本地开发

```bash
cd backend && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn src.main:app --reload --port 8000   # /api 前缀
cd frontend/site && python -m http.server 5173
```

## 6. 访问地址

| 服务 | 地址 |
|---|---|
| 前端 | http://localhost:18207 |
| 后端 API 文档 | http://localhost:19207/docs |
| 健康检查 | http://localhost:19207/api/health |
| 数据库映射 | localhost:17707 |

## 7. 技术栈

| 层 | 选型 |
|---|---|
| 前端 | 原生 HTML5 + CSS（oklch 令牌 / 网格纸底纹）+ ES Modules；无框架、无打包器、除 Google Fonts 外无 CDN |
| 后端 | Python 3.11、FastAPI 0.115、SQLAlchemy 2.0、PyJWT、passlib(bcrypt) |
| 算法 | 纯 `math` 手写：卡方上尾（不完全伽马）、正态分位数、双比例 z 检验、Bonferroni / BH-FDR |
| 异步 | Celery 5.4 + Redis（10s 心跳、07:40 每日 SRM 复算） |
| 数据库 | MySQL 8.0（utf8mb4） |
| 部署 | Docker Compose，恰好 5 服务（frontend / backend / worker / db / redis） |

## 8. 目录

```text
backend/
  src/{config,constants,controllers,middlewares,models,routes,schemas,services,utils}
  src/services/algorithms/{stats,srm,metric_test,exposure}.py   # 两个算法 + 统计底座 + 曝光区间
  src/services/analysis_service.py                      # 区间登记 / 整套发布 / 失效
  src/controllers/analysis_controller.py
  scripts/{seed_demo.py,demo_config.py,worker_healthcheck.sh}
  migrations/  tests/
frontend/site/{index.html,css/{tokens,app}.css,js/{config,api,store,ui,app}.js,js/pages/*.js}
```

## 9. 环境变量

见 `.env.example`：`COMPOSE_PROJECT_NAME`、`FRONTEND_PORT=18207`、`BACKEND_PORT=19207`、`DB_PORT=17707`、
`DB_*`、`JWT_SECRET`、`REDIS_URL`、`SEED_DEMO`、`RATE_LIMIT_*`、`APP_TIMEZONE_OFFSET_MINUTES`。

## 10. 核心算法与口径

### 分析版本｜可用曝光区间（`src/services/analysis_service.py` + `algorithms/exposure.py`）

**登记**：`POST /api/experiments/{id}/analysis-runs`，body 支持多段区间（闭区间、可留缺口）：

```json
{"segments": [{"start": "2026-06-01", "end": "2026-06-09"},
              {"start": "2026-06-12", "end": "2026-06-15"}],
 "note": "06-10/06-11 埋点故障，不进本次分析", "method": "bh"}
```

单段可简写 `interval_start` / `interval_end`。区间归一化后按开始日期升序、不允许重叠，
生成稳定指纹 `interval_key = "2026-06-01~2026-06-09|2026-06-12~2026-06-15"`。

**不变量**：

1. **区间唯一**：`analysis_versions` 上 `(experiment_id, interval_key)` 唯一。同一组区间重跑 → 复用同一版本，
   `revision` 不变、不新增行，界面提示「复用 vN」。每次整套发布把 `publication_count` +1，
   决策据此判断「结论是否还是这次发布算出来的」。
2. **整套发布**：一次 `analysis-runs` 在同一个 SAVEPOINT 里算出并写入 SRM 快照、**全部变体的全部指标**、
   建议与实验上的 active 指针；不存在「只更新了 SRM 却没更新指标」的中间态。
3. **旧版本立即失效**：区间调整发布成功后，该实验其它 `active` 版本立刻变 `superseded`，
   实验上的 `active_analysis_version_id` / `active_interval_segments` / SRM 快照同步切到新版本；
   同一版本同区间重跑则版本不变、只把 `publication_count` 前进一格。
4. **失败沿用上一版**：区间内没有样本（`NO_EXPOSURE` 422）、参数非法等任一失败 → 本次写入回滚，
   新版本保持 `draft` 并记 `failure_reason`，实验的 active 指针与快照全部不动，页面继续展示上一版结果。
5. **决策绑定版本 + 发布轮次**：决策落库写
   `analysis_version_id / analysis_version_revision / analysis_version_publication / interval_key`；
   只能对**当前生效版本的当前发布**出决策，传失效版本 → 409。区间调整（旧版本 superseded）
   或同区间重跑（版本不变但 `publication_count` 前进）后，旧决策自动失效、保留为历史：
   列表/详情标「已失效」，导出 `决策是否有效=已失效（需重新决策）`，总览计数与告警同步；
   可对当前发布重新出决策。
6. **导出版本可追溯**：`metrics.csv / decisions.csv / experiments.csv / assignments.csv` 均带
   `分析版本 v / 版本状态 / 可用曝光区间` 列；默认导出生效版本，`?analysis_version_id=` 可导出历史版本
   （`version_status` 列标明 `已失效`）。导出接口审计记录 `analysis_version_id`。

**区间过滤**：所有样本聚合（`variant_totals` / `channel_matrix` / `daily_series` / `metric_keys`）都带
`segments` 过滤，只有 `stat_date ∈ 区间` 的行参与；`/api/experiments/{id}/samples` 额外返回
`excluded_rows`（区间外行数），页面提示「被排除多少行」，避免整段累计。

**兼容入口**：旧的 `POST /srm-check`、`POST /metric-tests` 保留但**不再脱离版本**：它们要求该实验已有生效版本，
按生效版本的区间整套重算并就地覆盖，不会按整段累计、也不会新增版本。

**前端交互**：

- `#/assignments`：顶部「分析版本」面板显示生效版本与区间；「登记区间并整套发布」抽屉里每行写一段
  `2026-06-01~2026-06-09`，格式错误在客户端就拦下（不发请求）；页面另有版本台账，列出生效/失效/失败原因。
- `#/metrics`：版本选择器可在生效版本与历史版本间切换；查看失效版本时顶部标红「只能追溯，不能决策」，
  重跑按钮禁用。指标表每行显示所属版本 v。
- `#/decisions`：出决策必须选择「依据的分析版本」，失效版本提交被服务端 409 拒绝；决策列表标「已失效」；
  导出按钮旁注明 CSV 带版本列。
- `#/` 总览：台账列出每个实验的生效版本与区间；有失效决策时页面顶部黄色告警条提示重新出结论。

### 算法 A｜SRM 与功效（`src/services/algorithms/srm.py`）

- 输入：实验的变体列表（计划比例 `ratio`）+ **可用曝光区间内**各变体主指标分流样本合计 `observed` + 实验登记的基准转化率 `baseline_rate`、目标最小效应 `planned_mde`。
- 卡方：`χ² = Σ (obs_i - exp_i)² / exp_i`，`exp_i = N · ratio_i`，`df = k - 1`；
  p 值 = 卡方上尾概率 = 正则化上不完全伽马 `Q(df/2, χ²/2)`（级数 + Lentz 连分式，纯 math 实现）。
- 判定：`p < 0.001` → `srm`（SRM 告警），否则 `balanced`。
- 样本量：`n = 2(z_{1-α/2} + z_{1-β})² · p(1-p) / Δ²`，逐变体输出 `required` 与 `gap = max(0, n_required - n_observed)`。
- 功效与 MDE：`power = Φ(√(nΔ²/(2p(1-p))) - z_{1-α/2})`；`MDE = (z_{1-α/2}+z_{1-β})·√(2p(1-p)/n)`。
- 失败态：变体 < 2、样本合计为 0、计划比例合计为 0 → 422（`NO_SAMPLES` / `VALIDATION`）。

### 算法 B｜双比例检验 + 多重校正（`src/services/algorithms/metric_test.py`）

- 输入：同一分析版本同一变体下每个指标的 对照/实验 样本量与转化数（来自 `assignments` 按区间聚合）。
- 检验：`p̄ = (c₁+c₂)/(n₁+n₂)`，`z = (p₁-p₂)/√(p̄(1-p̄)(1/n₁+1/n₂))`，双侧 `p = 2(1-Φ(|z|))`。
- 区间：Wald 95% CI `Δ ± z_{1-α/2}·√(p₁(1-p₁)/n₁ + p₂(1-p₂)/n₂)`；lift CI 由同一区间除以 p₂。
- 校正：Bonferroni `q = min(1, m·p)`；BH-FDR 升序 `q_(i) = min_{k≥i}(m·p_(k)/k)` 并单调化；两者都落库。
- 落库：每个 `(analysis_version_id, 变体, 指标)` 一行；重跑同版本就地覆盖整套，版本之间互不覆盖。
- 结论：`q < 0.05` 且 Δ>0 → `ship`（上线）；`q < 0.05` 且 Δ<0 → `stop`（停止）；否则 `continue`（继续观察）。
- 失败态：样本量为 0、转化数越界、method 非 `bh|bonferroni` → 422。

两个算法都是纯函数（除落库层），同样输入两次逐位一致；`backend/tests/` 覆盖算法正常 / 边界 / 确定性、
曝光区间归一化与重叠校验、以及分析版本端到端不变量（重跑不新增版本 / 区间调整旧版失效 / 失败沿用上一版 /
决策与导出可追溯）共 40+ 用例。

## 11. 共享枚举出现位置

| 枚举 | 后端 | 前端 |
|---|---|---|
| 角色 `admin/analyst/viewer` | `backend/src/constants/{roles,enums}.py` | `frontend/site/js/config.js`（`ROLE_LABELS` / `PERMISSIONS`） |
| 实验状态 `draft/running/paused/finished/archived` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.experimentStatus`） |
| 决策结论 `ship/continue/stop` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.verdict`） |
| SRM 状态 `balanced/srm/unknown` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.srm`） |
| 渠道 `feed/push/search/mp` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.channel`） |
| 指标口径 `cart_rate/order_rate/retention_d1/click_rate/search_success` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.metric`） |
| 校正方法 `bh/bonferroni` | `backend/src/constants/enums.py` | `frontend/site/js/config.js`（`ENUMS.correction`） |
| 分析版本状态 `draft/active/superseded` | `backend/src/services/analysis_service.py`（`STATUS_LABELS`） | `frontend/site/js/config.js`（`ENUMS.analysisStatus`） |
| 决策自然键 `(experiment, version, publication)` | `backend/src/models/decision.py` | —（前端展示 `vN.M` 与失效标记） |

## 12. Docker 说明

- 5 个服务：`frontend`(Nginx 静态站) / `backend`(uvicorn) / `worker`(Celery) / `db`(MySQL 8.0) / `redis`，全部带 healthcheck。
- 新增表 `analysis_versions` 由启动时 `Base.metadata.create_all()` 自动建；历史库需手工加列
  （`experiments.active_*`、`metric_results.analysis_version_id`、`decisions.analysis_version_id` 等），
  参考 `migrations/001_core_tables.sql`。
- 命名卷：`db_data`、`redis_data`；`depends_on: service_healthy`；端口 `${FRONTEND_PORT:-18207}` 写法。
- Nginx：`location /api/ { proxy_pass http://backend:8000/; }` + SPA 回落。
- 收尾：`docker compose down -v --remove-orphans`。

## 13. 视觉方向

实验室记录本：石墨灰墨色（`--ink oklch(0.285 0.012 250)`）+ 芥末黄（`--accent oklch(0.745 0.135 85)`）+
网格纸底纹（body 双层 grid 渐变）、手写批注（Caveat，`.note`）、印章（`.stamp`）；
登录页做成「实验登记页」（Lab Sheet A-2607，表头 + 受控章 + 手写提示），总览做成台账 + 告警页。

## License

内部演示项目，仅用于评测与教学。
