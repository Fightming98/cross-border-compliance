# 跨境商品合规检查助手

一个面向本地演示与测试的跨境商品合规检查工作台：读取 120 条待检 SKU 清单，以只读方式关联 SQLite 商品库，按规则库判定合规风险、按数据库汇率换算人民币价格，并输出可追溯的 Markdown 报告、独立 HTML 报告、CSV 明细与交互式风险看板。

第五阶段在**不改动核心业务模块**的前提下，新增了可复用的应用服务、Flask 本地 Web 应用、原生 HTML/CSS/JS 前端、单文件离线 HTML 报告、任务结果存储、结构化 API、轮转日志和 53 个新测试。当前全量 **174 个测试全部通过**，原有 121 个测试保持通过。

> 本项目是本地演示与测试工具，不是已经完成公网部署的生产服务。默认仅监听 `127.0.0.1`，没有身份认证、HTTPS、限流或 CSRF 防护，不应直接暴露到公网。

> 提交物索引：四件套对应关系见 [`提交说明.md`](提交说明.md)；一页《踩坑与验收说明》见 [`docs/踩坑与验收说明.md`](docs/踩坑与验收说明.md)；AI 原始会话仅随提交压缩包提供（`ai_records/`），公开仓库不包含。

## 1. 项目简介

- **输入**：带 UTF-8 BOM 的 `sku_list.csv`，保留 120 条原始记录与 `batch_no`；
- **数据源**：项目根目录的 `compliance.db`，只读打开，读取商品、规则与汇率；
- **业务处理**：SKU 映射、处置状态判定、三条件规则匹配、`Decimal + ROUND_HALF_UP` 人民币换算、完整性校验；
- **四种查看方式**：CLI 输出、Markdown 报告、独立 HTML 报告、本地 Web 看板；
- **三种导出**：Markdown、HTML、CSV，全部来自同一次后端检查结果；
- **可追溯性**：任务随机 ID、结构化错误编号、轮转运行日志、任务结果快照；
- **离线可用**：前端不使用 CDN、远程字体、网络图片或在线图表服务。

当前正式数据基线：

```text
清单读取数：120
在库数（含已下架）：115
正常评估数：110
已下架数：5
库中缺失数：5
风险统计：合规 47 / 待确认 56 / 违规 7
```

## 2. 技术架构

| 层 | 实现 |
|---|---|
| 业务核心 | Python 标准库；原有 `loader`、`processor`、`rules_engine`、`pricing`、`validation`、`report` |
| 应用服务 | `application_service.py`：统一执行一次检查，供 CLI、Web、HTML、CSV 复用 |
| Web 后端 | Flask 3.1.3；JSON API、Jinja2 模板、multipart 上传、任务存储、日志 |
| 前端 | HTML5 + 原生 CSS + 原生 JavaScript；`fetch`；CSS 图表；响应式布局 |
| 数据 | SQLite 3（只读，`?mode=ro`）；正式数据库固定为 `compliance.db` |
| 报告 | Markdown（原有）、单文件离线 HTML、UTF-8 BOM CSV |
| 测试 | `unittest`；Flask 测试客户端；真实 HTTP 冒烟；可选 Playwright + 本机 Chrome |

统一数据流：

```text
CSV + 只读 SQLite
        ↓
    现有 loader
        ↓
   现有 processor
        ↓
 rules_engine / pricing
        ↓
    现有 validation
        ↓
  ComplianceRun（统一结果对象）
        ↓
 ┌──────────┬──────────┬──────────┬──────────┐
 │ Markdown │ HTML 报告 │ Web 看板 │ CSV 明细 │
 └──────────┴──────────┴──────────┴──────────┘
```

所有出口只做“结果对象 → 展示格式”的转换，不在前端、HTML 模块或 CSV 模块中重新实现规则匹配、风险优先级或价格换算。

## 3. 项目模块图

```text
.
├── instructions.md                     # 原始题目，未修改
├── data-dictionary.md                  # 原始字典，未修改
├── sku_list.csv                        # 原始清单，未修改
├── compliance.db                       # 原始数据库，只读访问
├── seed.py                             # 原始种子代码，未修改、未复用
├── README.md
├── requirements.txt                    # Flask==3.1.3（核心业务仍只用标准库）
├── main.py                             # CLI 入口
├── web_app.py                          # Flask 应用工厂与本地启动入口
├── src/compliance_assistant/
│   ├── errors.py                       # 统一异常类型
│   ├── models.py                       # 数据模型与枚举
│   ├── loader.py                       # CSV / SQLite 只读加载与结构校验
│   ├── rules_engine.py                 # 三条件规则匹配与风险聚合
│   ├── pricing.py                      # Decimal 人民币换算
│   ├── processor.py                    # 映射、状态判定与流程编排
│   ├── validation.py                   # 输入、结果与完整性校验
│   ├── report.py                       # 原有 Markdown 报告
│   ├── application_service.py          # 统一应用服务、JSON 载荷、CSV 导出
│   ├── html_report.py                  # 单文件离线 HTML 报告
│   ├── job_store.py                    # 随机任务 ID、固定目录、路径防护、清理
│   └── logging_config.py               # 控制台 + 轮转文件日志
├── templates/
│   ├── base.html                       # 公共布局与本地资源
│   ├── index.html                      # 首页
│   ├── _dashboard.html                 # 交互看板片段
│   ├── results.html                    # 服务端结果快照页
│   └── error.html                      # 友好错误页
├── static/
│   ├── css/app.css                     # 原生样式、响应式布局、图表样式
│   ├── js/app.js                       # 上传、图表、筛选、排序、分页、下载
│   └── images/logo.svg                 # 本地 SVG 标识
├── tests/                              # 174 个测试（原有 121 + 新增 53）
│   ├── test_application_service.py     # 11 个
│   ├── test_html_report.py             # 11 个
│   └── test_web_app.py                 # 31 个
├── scripts/
│   ├── run_stage4_acceptance.py        # 第四阶段一键验收（历史）
│   ├── run_stage5_web_smoke.py         # 真实 HTTP 服务冒烟
│   ├── run_stage5_browser_smoke.py     # 真实浏览器冒烟（可选依赖）
│   └── check_stage5_cross_entry.py     # Markdown/HTML/CSV 独立核对
├── reports/
│   ├── compliance_report.md            # CLI 正式 Markdown 报告
│   ├── compliance_report.html          # 独立 HTML 报告
│   ├── compliance_details.csv          # CSV 明细
│   ├── web_runs/<job_id>/              # Web 任务结果快照
│   └── screenshots/                    # 浏览器验收截图
├── logs/application.log                # Web 运行日志（轮转）
├── docs/                               # 阶段文档与 AI 协作索引
├── evidence/stage4/                    # 第四阶段证据（不覆盖）
└── audit/                              # 第一阶段审计产物，未修改
```

## 4. 安装依赖

环境要求：

- Python **3.10+**（实际验证：Python 3.12.4）；
- SQLite（Python 标准库自带）；
- 浏览器验收可选：Playwright + 本机 Chrome；它们**不是**运行 Web 应用的依赖，因此未写入 `requirements.txt`。

安装 Web 依赖：

```bash
python -m pip install -r requirements.txt
```

`requirements.txt` 只声明：

```text
Flask==3.1.3
```

Flask 会安装 Werkzeug、Jinja2、MarkupSafe、itsdangerous、click、blinker 等运行依赖。核心业务模块不依赖 Flask，仍可只用 Python 标准库执行。

## 5. CLI 运行方法

在项目根目录执行：

```bash
python main.py
```

默认读取：

- `sku_list.csv`
- `compliance.db`

默认输出：

- `reports/compliance_report.md`

可选参数：

```bash
python main.py --csv sku_list.csv --db compliance.db --output reports/compliance_report.md
python main.py --expected-count 120
```

- `--csv`：输入清单路径；
- `--db`：SQLite 数据库路径；
- `--output`：Markdown 输出路径（父目录会自动创建）；
- `--expected-count`：期望记录数，默认 `120`；CLI 正式验收使用默认值。

退出码：

- `0`：处理、校验、报告生成与落盘复核全部成功；
- `1`：输入/数据库/规则/价格/完整性/报告错误，或发生未预期异常；
- `2`：命令行参数非法（由 `argparse` 返回）。

## 6. HTML 报告与 CSV 明细生成

保留默认行为的前提下，CLI 可选择同时生成 HTML 和 CSV：

```bash
python main.py \
  --html-output reports/compliance_report.html \
  --csv-output reports/compliance_details.csv
```

- `--html-output`：生成单文件离线 HTML 报告；
- `--csv-output`：生成 UTF-8 BOM 的 120 行明细 CSV；
- 不传这两个参数时，行为与原 CLI 完全一致，只生成 Markdown。

HTML 报告包含：

- 报告标题、生成时间、输入文件、任务状态、报告编号；
- 8 项概览统计；
- 风险分布、处置分布、Top 风险类目；
- 120 条完整 SKU 明细；
- 完整性等式与校验结果；
- 风险等级、处置状态、人民币换算与统计口径说明。

安全与离线特性：

- 单文件 HTML，CSS 内嵌，不使用 JavaScript；
- 不引用 CDN、远程字体、网络图片、在线图表服务；
- 所有 CSV/SQLite 文本经 `html.escape` 转义；
- 生成前后校验明细行数、必需区块和外部资源引用；
- 原子写入，失败时清理临时文件。

## 7. Web 应用启动方法

```bash
python web_app.py
```

默认参数：

- 监听地址：`127.0.0.1`（固定，不可通过参数改到 `0.0.0.0`）；
- 端口：`5000`；
- Flask `debug=False`，`use_reloader=False`，不启用交互式调试器。

指定其他本地端口：

```bash
python web_app.py --port 5001
```

端口被占用时，程序会输出明确中文错误并返回非零退出码，不会自动改绑其他网络地址。

启动时会：

1. 初始化 `logs/application.log`；
2. 清理 `reports/web_runs/` 中超过 7 天的合法任务目录；
3. 记录 `app_start` 事件（版本、地址、端口、数据库、结果目录、保留期）。

## 8. 本地访问地址

启动后在浏览器打开：

```text
http://127.0.0.1:5000
```

任务页面与 API 使用随机任务 ID：

```text
http://127.0.0.1:5000/?job=<32位十六进制任务ID>
http://127.0.0.1:5000/reports/<32位十六进制任务ID>
```

服务不会默认监听 `0.0.0.0`，也不会自动开放到局域网。

## 9. Web 上传的数据格式要求

- 仅允许 `.csv` 文件；
- 文件必须非空，最大 **2 MB**（请求总大小限制约 2 MB + 64 KB）；
- 编码支持 UTF-8 / UTF-8 BOM；
- 必须包含表头 `sku_ref` 与 `batch_no`；
- `sku_ref` 必须为五位数字；
- `batch_no` 不可为空；
- 重复行按输入行独立保留，不去重、不静默丢行；
- 未在商品库中的 SKU 会保留为“库中缺失”，不会导致整批失败；
- 数据库固定使用项目根目录的 `compliance.db`，客户端不能上传或指定其他数据库；
- 浏览器提供的文件名只用于界面显示，会被清理路径与控制字符，不用于服务器文件路径。

## 10. API 路由说明

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 返回上传与交互看板首页 |
| `POST` | `/api/reports` | `multipart/form-data` 上传 CSV，字段名 `file`（兼容 `csv`） |
| `GET` | `/api/reports/<job_id>` | 返回任务统计、明细、Top 类目、完整性与下载地址 |
| `GET` | `/reports/<job_id>` | 返回服务端渲染的只读结果快照页 |
| `GET` | `/api/reports/<job_id>/download/markdown` | 下载 Markdown 报告 |
| `GET` | `/api/reports/<job_id>/download/html` | 下载独立 HTML 报告 |
| `GET` | `/api/reports/<job_id>/download/csv` | 下载 CSV 明细 |
| `DELETE` | `/api/reports/<job_id>` | 删除该任务的全部结果文件 |

上传成功响应包含：

- `job_id`、`status=completed`、`version`、`generated_at`；
- `source`：显示文件名、SHA-256、字节数；
- `summary`：8 项统计、风险分布、处置分布、风险比例；
- `integrity`：统计等式与风险总数等式；
- `top_categories`；
- `rules`：规则编号、标题、严重度、匹配字段；
- `details`：120 条明细及命中规则；
- `links`：自身、结果页、三类下载地址。

错误响应统一为：

```json
{
  "ok": false,
  "error": {
    "code": "invalid_csv",
    "message": "CSV 校验失败：……",
    "error_id": "ERR-XXXXXXXX"
  }
}
```

状态码约定：

- `400`：缺文件、空文件、扩展名错误、CSV/清单校验失败；
- `404`：任务不存在、已删除、已过期，或下载文件不存在；
- `405`：方法不允许；
- `413`：文件或请求超过限制；
- `500`：数据库、报告生成或存储等可预期服务端失败。

错误响应不包含 Python traceback；内部异常写入本地日志，并带错误编号。

## 11. 风险图表和明细交互说明

上传成功后，首页展示：

- **8 张统计卡**：清单读取数、在库数、正常评估数、已下架数、库中缺失数、合规、待确认、违规；
- **3 个图表**：风险等级分布、处置状态分布、Top 风险类目；
- **完整明细表**：120 条，默认每页 20 条。

图表与表格：

- 每根条显示具体数量，并附文本数据表（`<details>`）；
- 风险使用“颜色 + 图标 + 文字”，不只依赖颜色；
- 图表有标题、图例、比例说明和 `aria-label`；
- 窄屏下图表改为单列，明细表在容器内横向滚动，不产生页面级横向溢出。

明细交互：

- 按 SKU 或商品名称搜索；
- 按处置结果、风险等级、类目筛选；
- 组合筛选与空状态提示；
- 按列排序，`aria-sort` 同步；
- 分页、上一页/下一页、页码跳转；
- 展开命中规则，查看规则编号、标题、严重度与匹配条件。

前端只改变展示，不重新执行规则判定；所有统计和明细来自后端响应。

## 12. 报告下载方式

在 Web 看板的“导出与任务清理”区域，或结果快照页顶部，可下载：

- Markdown：`compliance_report.md`；
- HTML：`compliance_report.html`；
- CSV：`compliance_details.csv`。

对应 API：

```text
GET /api/reports/<job_id>/download/markdown
GET /api/reports/<job_id>/download/html
GET /api/reports/<job_id>/download/csv
```

下载文件来自服务端允许列表，客户端不能指定文件名或路径；下载响应使用 `Cache-Control: no-store`。

CLI 生成方法见第 6 节。Web 的 Markdown/CSV 与 CLI 文件逐字节一致；HTML 因包含任务编号与生成时间，字节不同，但 120 行明细逐字段一致。

## 13. 日志路径

默认日志文件：

```text
logs/application.log
```

日志同时输出到控制台和文件，使用 Python `logging`：

- 轮转大小：1,000,000 字节；
- 备份数量：5；
- 格式：时间、级别、任务 ID、事件；
- 记录任务开始、上传大小、各阶段数量、报告生成、耗时、成功/失败、删除、下载和异常；
- 不记录完整 CSV、整表数据库内容或完整商品明细；
- 错误响应带 `ERR-` 编号，可和日志中的 `error_id` 对应；
- 测试使用临时日志目录，不污染正式日志。

示例（真实日志节选）：

```text
2026-10-24 09:39:54 | INFO    | job=ab3fb8... | event=task_received
2026-10-24 09:39:54 | INFO    | job=ab3fb8... | event=check_completed n_input=120 ...
2026-10-24 09:39:55 | INFO    | job=ab3fb8... | event=task_completed duration_ms=131.6
```

## 14. 报告和任务结果保存路径

CLI：

- Markdown：`reports/compliance_report.md`；
- HTML（指定时）：`reports/compliance_report.html`；
- CSV（指定时）：`reports/compliance_details.csv`。

Web：

```text
reports/web_runs/<job_id>/
├── result.json                 # 统一结果快照
├── compliance_report.md
├── compliance_report.html
└── compliance_details.csv
```

上传的原始 CSV 只写入系统临时目录（`compliance_upload_*`），在请求结束后删除，不永久保存。

任务结果包含 SKU、批次、商品名、价格和规则等业务信息，均保存在本机上述目录，不会发送到外部服务。

## 15. 文件保留与清理方式

- Web 任务默认保留 **7 天**；
- 启动时清理超过 7 天的合法任务目录；
- 界面“删除任务结果”调用 `DELETE /api/reports/<job_id>`，删除任务目录及全部文件；
- 删除后再次访问该任务返回 404；
- 当前没有后台定时清理线程，清理动作发生在启动时；运行期间可随时手动删除；
- 第四阶段证据目录 `evidence/stage4/` 不被覆盖；
- 原始 `compliance.db`、`sku_list.csv`、`instructions.md`、`data-dictionary.md`、`seed.py`、`audit/` 保持不变。

## 16. 本地安全边界

已实现：

- 默认且固定绑定 `127.0.0.1`；
- `debug=False`，无交互式调试器；
- 数据库路径、结果目录、日志路径由服务端配置固定，客户端参数被忽略；
- 上传仅接受 CSV，限制 2 MB，流式写入并检查空文件；
- 文件名只用于显示，不参与服务器路径；
- 任务 ID 为 32 位小写十六进制（128 位随机数）；
- 严格校验任务 ID 与允许的报告类型，防止路径穿越；
- 任务结果只允许三个固定文件名；
- 不返回 traceback，统一结构化错误；
- CSP、`X-Content-Type-Options`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`；
- API 与结果下载使用 `Cache-Control: no-store`；
- 前端不加载外部 CDN/字体/图片/在线图表，不使用 `innerHTML` 或 `eval` 渲染业务数据；
- 日志不记录完整上传内容。

未实现（因此不能视为生产就绪）：

- 身份认证、用户隔离、CSRF Token、限流、HTTPS、反向代理；
- 公网部署所需的审计、密钥管理、备份和多用户权限；
- 面向不可信网络的防护承诺。

## 17. 测试执行方法

### 17.1 编译与全量测试

```bash
python -m compileall -q main.py web_app.py src tests scripts
python -m unittest discover -s tests -v
```

当前实测：

```text
Ran 174 tests in 4.241s

OK
```

测试构成：

- 原有测试：121 个（不变）；
- 应用服务：11 个；
- HTML 报告：11 个；
- Web 路由/安全/日志：31 个；
- 合计：174 个。

全量输出留档：`reports/stage5_final_tests.txt`。

### 17.2 真实 HTTP 冒烟

```bash
python scripts/run_stage5_web_smoke.py --port 5000
```

覆盖：首页与安全响应头、本地静态资源、真实 multipart 上传、API/结果页/三类下载、404/405/400、任务删除、日志字段、`127.0.0.1` 绑定、CLI 与 Web 业务一致性。

输出：

- `reports/stage5_web_smoke.json`
- `reports/stage5_web_smoke.txt`
- `reports/stage5_web_server.log`

### 17.3 真实浏览器冒烟（可选）

环境已安装 Playwright 并使用本机 Chrome：

```bash
python scripts/run_stage5_browser_smoke.py --port 5000
```

覆盖：首页、文件选择、真实上传、8 张统计卡、3 个图表、搜索/筛选/排序/分页、规则展开、三类下载、错误状态、结果快照页、390px 窄屏、任务删除、外部请求与控制台异常检查。

截图与摘要：

- `reports/screenshots/stage5_home_desktop.png`
- `reports/screenshots/stage5_dashboard_desktop.png`
- `reports/screenshots/stage5_dashboard_mobile.png`
- `reports/screenshots/stage5_snapshot_desktop.png`
- `reports/screenshots/stage5_upload_error_desktop.png`
- `reports/stage5_browser_smoke.json`
- `reports/stage5_browser_smoke.txt`

Playwright 不是本应用的运行依赖；没有该环境时，Web 应用、CLI 和全部单元测试仍可正常运行。

### 17.4 跨入口核对

```bash
python scripts/check_stage5_cross_entry.py \
  --output reports/stage5_cross_entry_check.txt
```

该脚本独立读取 Markdown、HTML、CSV，逐字段核对 120 条明细、汇总与完整性等式，并检查 HTML 无脚本/外部资源。

## 18. 已验证基线与验收结果

### 18.1 业务基线

| 项目 | 数值 |
|---|---:|
| 清单读取数 | 120 |
| 在库数（含已下架） | 115 |
| 已下架数 | 5 |
| 库中缺失数 | 5 |
| 正常评估数 | 110 |
| 合规 | 47 |
| 待确认 | 56 |
| 违规 | 7 |

完整性等式：

```text
120 = 115 + 5
115 = 5 + 110
120 = 5 + 5 + 110
47 + 56 + 7 = 110
```

### 18.2 跨入口一致性

- Markdown、HTML、CSV 三份导出各 120 条明细，逐字段一致；
- Web 下载的 Markdown/CSV 与 CLI 文件逐字节一致；
- HTML 120 行明细与 CLI/CSV 一致；因包含任务编号和生成时间，文件哈希不同；
- API、Markdown、HTML、CSV 四类表示均通过 120 行逐字段核对；结果快照页确认 120 行、8 张统计卡和三类下载链接；
- 当前文件哈希：

```text
Markdown  d3d5682193a6babdfc6a8ddc925b6003046154b8795d2103092169ecf72be8be
HTML      13307395115f109c3bcc2369fba03d124ee65e7a1abad9c314bdc1785268c7bf
CSV       c308eeb332e2ad75b05afc95b746717ea2d3c524ebcc569448de2352204f24e2
```

HTML 哈希会随生成时间和报告编号变化，业务内容以逐字段核对为准。

### 18.3 原始文件完整性

以下 SHA-256 与第一阶段审计基线一致：

```text
instructions.md     a6870dfb697615b2ced53dc23af2c8e04b863d559aa3a107c7ac0142836db7a1
data-dictionary.md  3897cc8881d9e08b27d1a2059f50ca2a06f4960693ba4953dc027d96bd078c14
seed.py             834f7f748e7b4db9b626642c4be59ffb278e5a2df4e43d884bfdf4c3b7b77399
sku_list.csv        bc146ce7c529d34b3b2f741209c45ffc5366ae775c64579e2363986d6b653da9
compliance.db       e87eb1b9be1a53f547d45af27b49017e01f63d63a8903eb9c717193af9f9ad67
```

数据库未产生 `-wal` / `-shm` / `-journal` sidecar；第四阶段 `evidence/stage4/20261022_233849/SHA256SUMS` 全部校验通过，未被覆盖。

### 18.4 实际执行记录

| 命令 | 结果 |
|---|---|
| `python -m compileall -q main.py web_app.py src tests scripts` | 退出码 0 |
| `python -m unittest discover -s tests -v` | 174 个测试全部通过，`OK`，4.241s |
| `python main.py` | 退出码 0；120/115/5/5/110；47/56/7 |
| `python main.py --html-output ... --csv-output ...` | 退出码 0；HTML/CSV 生成成功 |
| `python scripts/run_stage5_web_smoke.py --port 5000` | 退出码 0；全部 HTTP 检查通过 |
| `python scripts/run_stage5_browser_smoke.py --port 5000` | 退出码 0；桌面与 390px 窄屏通过 |
| `python scripts/check_stage5_cross_entry.py` | 退出码 0；三入口 120 行逐字段一致 |

详细设计与验收记录见 `docs/第五阶段产品化设计与验收报告.md`。

## 19. 关键业务口径

### 19.1 SKU 映射

```text
mapped_sku_id = "SKU-" + sku_ref.strip()
```

- `sku_ref` 必须匹配 `^\d{5}$`，否则显式报错；
- 不补零、不截断、不猜测；
- 报告保留 CSV 原始 `sku_ref` 与 `batch_no`；
- 当前基线：120 条中 115 条可关联，5 条库中缺失。

### 19.2 商品状态

判定顺序固定为：

1. 数据库中不存在：`库中缺失`；
2. 存在且 `is_deleted == 1`：`已下架`；
3. 存在且 `is_deleted == 0`：`正常评估`；
4. 其他 `is_deleted` 取值：显式报错。

只有“正常评估”商品进入规则引擎和价格模块。已下架/缺失商品不会被标记为“合规”。

### 19.3 规则匹配

一条规则生效当且仅当：

```text
类目匹配 AND 材质匹配 AND 产地匹配
```

- 类目：按英文逗号拆分、逐项 `strip` 后**精确成员匹配**；
- 材质：`material_keyword` 为 `NULL`/空串/纯空白时视为不限；否则按 `|` 拆分，任一关键词为商品材质**子串**即通过；
- 产地：`origin_keyword` 为 `NULL`/空串/纯空白时视为不限；否则与 `origin.strip()` **完全相等**；
- 风险优先级：`违规 > 待确认 > 合规`；
- 命中多条规则时保留**全部**规则编号，按 `rule_id` 升序，用英文分号 `;` 连接；
- 未知 `severity`、空类目、空 token、空规则表均显式报错。

### 19.4 人民币换算

```text
人民币价格 = 商品原价 × exchange_rates.rate_to_cny
```

- 汇率只从数据库读取，不硬编码；
- 使用 `Decimal(str(value))`；
- 使用 `ROUND_HALF_UP` 保留 2 位小数；
- CNY 也使用数据库中的 `CNY` 行（当前为 `1.0`）；
- 缺少汇率、价格/汇率非法时显式报错，不跳过商品；
- 已下架/缺失商品不换算。

### 19.5 完整性口径

- **在库数** = 能够关联到商品库的输入记录数，**包含已下架**；
- 五个数字由实际处理结果动态计算，不硬编码；
- 三条等式：

```text
清单读取数 = 在库数 + 缺失数
在库数 = 已下架数 + 实际评估数
清单读取数 = 缺失数 + 已下架数 + 实际评估数
```

### 19.6 Top 风险类目

1. 仅统计至少有一件“违规”或“待确认”商品的类目；
2. 每个正常评估 SKU 按最终风险只计一次；
3. 违规数降序；
4. 违规数相同则待确认数降序；
5. 仍相同则类目名升序；
6. 最多 5 个；
7. 同时显示违规数、待确认数、风险商品总数；
8. 没有风险类目时明确显示“没有风险类目”。

## 20. 待确认事项的实现假设

第一阶段审计提出的 Q1–Q6 在本实现中的处理如下。以下均为**实现假设**，不是已经获得业务方书面确认的事实。

| ID | 假设 |
|---|---|
| Q1 | `is_deleted=1` 按“已下架”处理；依据字段名、取值分布和题目语境推断 |
| Q2 | “在库数”= 可关联记录数（含已下架）= 115；同时展示 110 与三条等式 |
| Q3 | 保留 2 位小数采用 `ROUND_HALF_UP` |
| Q4 | 缺失商品相关字段填“—”；已下架商品保留商品信息和原价，人民币价格/风险/规则填“—（不适用）” |
| Q5 | Top 风险类目采用上述固定排序，取前 5 |
| Q6 | JPY/KRW 金额偏低按数据库原值照常换算，不修改数据；在踩坑说明中记录 |

## 21. 已知限制

1. 这是**本地演示与测试应用**，使用 Flask 开发服务器，不具备公网生产部署能力；
2. 没有登录、用户隔离、CSRF Token、限流、HTTPS；任何能访问本机服务的人都可以使用；
3. 任务 ID 不可预测，但拿到完整 URL 即可访问对应结果；不要在不可信网络共享任务链接；
4. 请求同步处理，未引入任务队列；上传上限 2 MB，适合当前 120 行清单，不适合大规模批处理；
5. 7 天清理只在启动时执行，无后台定时线程；
6. `is_deleted` 的业务语义、JPY/KRW 价格量级仍需业务方确认；
7. HTML 报告因包含生成时间/任务编号，每次生成哈希不同；跨入口一致性以逐字段核对为准；
8. 浏览器验收依赖本机 Chrome 与 Playwright；未安装时不会自动执行，应用本身不受影响；
9. CSP 的 `style-src` 允许内联样式以支持图表宽度和模板样式，`script-src` 仍严格限制为 `'self'`，页面不使用内联脚本；
10. 第三阶段修复的 7 个问题均已回归验证；当前未发现未解决的业务代码缺陷，但不对未知场景作绝对保证。
