# AI 协作记录索引

> 本文件是**实际 AI 协作过程的索引**，用于把各轮提示、实现步骤、测试命令和产出对应起来。
> 它不是原始对话导出，也不冒充截图或日志；真实会话原始 JSONL 随提交压缩包放在 `ai_records/`（公开仓库不包含该目录）。
> 本索引没有把“计划”写成“已完成”，也没有伪造命令输出、测试结果或浏览器结果。

## 一、使用的任务提示

- 第一轮审计提示：`Prompt/01数据审计与需求确认.md`
- 第二轮编码提示：`Prompt/02核心业务编码实现.md`
- 第三轮审查提示：`Prompt/03独立代码审查与缺陷修复.md`
- 第四轮验收提示：`Prompt/04正式运行验收与证据留存.md`
- 第五轮产品化提示：`Prompt/05.产品化增强.md`
- 原始题目：`instructions.md`

## 二、正式编码前的输入材料

实际读取并作为实现契约的材料：

- `audit/00_审计总览.md`
- `audit/01_需求与验收清单.md`
- `audit/02_数据库Schema.md`
- `audit/03_SKU映射审计.md`
- `audit/04_规则库审计与业务口径.md`
- `audit/05_汇率与价格数据审计.md`
- `audit/06_材料差异与种子代码审查.md`
- `audit/07_数据审计汇总.json`
- `instructions.md`
- `data-dictionary.md`
- `sku_list.csv`、`compliance.db`、`seed.py`
- 第四阶段报告与证据：`docs/第四阶段运行验收与证据报告.md`、`evidence/stage4/20261022_233849/`

第五阶段开始前重新检查了核心源码、测试、报告、Git 状态和已有文档，并确认：

- 第四阶段正式验收的业务基线为 120/115/5/5/110、47/56/7；
- 原有 121 个测试是必须保留的回归基线；
- 六个核心业务模块不修改；
- 原始文件、`audit/`、第四阶段证据不覆盖；
- 当前目录是 Git 仓库，未擅自初始化、重置或提交。

## 三、实际实现步骤

### 3.1 第一至第四阶段

1. 从审计材料提炼实现契约：真实 Schema、`SKU-` 映射、三种处置状态、三条件规则、`CNY` 汇率、五数完整性口径。
2. 创建 `src/compliance_assistant/`：`models.py`、`errors.py`、`loader.py`、`rules_engine.py`、`pricing.py`、`processor.py`、`validation.py`、`report.py`。
3. 创建 `main.py`：默认路径、命令行参数、错误处理和退出码。
4. 创建测试，第二阶段达到 97 个测试；执行 `python main.py` 生成 Markdown，核对 120/115/5/5/110 与 47/56/7。
5. 第三阶段重新阅读全部生产代码、测试、文档和 Git 状态，编写独立参考实现逐 SKU 复算；修复 4 个 P1、3 个 P2 问题，测试增至 121 个。
6. 第四阶段编写 `scripts/run_stage4_acceptance.py`，以真实子进程执行编译、全量测试、独立对账和正式运行，生成 `evidence/stage4/20261022_233849/`。
7. 首轮证据发现 Windows 文本模式把 `SHA256SUMS` 写成 CRLF，导致 Git Bash `sha256sum -c` 失败；改为显式 LF 后重新执行验收，旧证据目录保留。
8. 编写 `docs/第四阶段运行验收与证据报告.md`，记录环境、命令、测试、对账、原始文件完整性和最终结论。

### 3.2 第五阶段

9. 阅读 `Prompt/05.产品化增强.md`、现有核心源码、测试、模板需求、第四阶段报告和 Git 状态，确定“核心业务不动、新增适配层”的实现边界。
10. 新增 `application_service.py`：
    - `run_compliance_check()` 统一调用原有 loader / processor / validation；
    - 定义不可变 `ComplianceRun`，包含 120 条结果、统计、规则、汇率、源文件 SHA-256、时间、风险/处置计数与 Top 类目；
    - `build_result_payload()` 提供 API 与任务快照共用的 JSON；
    - `build_details_csv()` / `write_details_csv()` 提供带 BOM 的 15 列 CSV，原子写入并复核行数。
11. 重构 `main.py` 使用应用服务，保留原有默认 CLI 行为，新增可选 `--html-output` 与 `--csv-output`。
12. 新增 `html_report.py`：
    - 单文件 HTML，CSS 内嵌、无 JavaScript、无外部资源；
    - 8 项统计、三类分布、Top 类目、120 行明细、完整性等式和底部口径；
    - 全部文本 `html.escape`；生成时检查章节/行数/外部引用，原子写入并复读。
13. 新增 `web_app.py`：
    - Flask 应用工厂与 `python web_app.py` 入口；
    - 固定 `127.0.0.1`，`debug=False`、`use_reloader=False`；
    - 上传/查询/结果页/三类下载/删除共 8 条路由；
    - 分块流式上传，2 MB 文件限制、2 MB + 64 KB 请求限制；
    - 统一 JSON 错误、错误编号、CSP 与安全响应头、`no-store`；
    - 每个任务生成 Markdown、HTML、CSV 和 `result.json`。
14. 新增 `job_store.py` 与 `logging_config.py`：
    - 128 位随机任务 ID、目录边界、允许列表、快照校验、删除、7 天启动清理；
    - 控制台 + 1 MB × 5 轮转文件日志，统一任务 ID 字段，不记录完整 CSV。
15. 新增前端：
    - `templates/base.html`、`index.html`、`_dashboard.html`、`results.html`、`error.html`；
    - `static/css/app.css`、`static/js/app.js`、`static/images/logo.svg`；
    - 8 张统计卡、3 个原生 CSS 图表、120 行明细、20 条/页；
    - 搜索、处置/风险/类目筛选、排序、分页、规则展开、三类下载、加载/错误状态；
    - 所有 API 文本用 `textContent` 写入，不使用 `innerHTML`；窄屏适配 390px/320px。
16. 新增 53 个测试：
    - `test_application_service.py` 11 个；
    - `test_html_report.py` 11 个；
    - `test_web_app.py` 31 个。
17. 新增验收脚本：
    - `run_stage5_web_smoke.py`：真实 HTTP 服务、上传、API、下载、错误、删除、日志、绑定检查；
    - `run_stage5_browser_smoke.py`：真实 Chrome + Playwright 的桌面/窄屏交互、下载、截图；
    - `check_stage5_cross_entry.py`：不导入业务模块，独立解析 Markdown/HTML/CSV 并逐字段比较。
18. 修复验收中发现的问题：
    - 32 位任务 ID 造成 390px 页面 5px 横向溢出；
    - Playwright 删除后等待使用 CSP 禁止的字符串求值；
    - Chrome 下载交接产生的 `ERR_ABORTED` 被误判为失败请求；
    - 整页截图对 sticky 顶栏的分段绘制伪影；
    - 窄屏明细标题与计数拥挤。
19. 实际执行编译、174 个测试、CLI、HTML/CSV 生成、跨入口核对、HTTP 冒烟和浏览器冒烟；记录真实输出。
20. 更新 README、第五阶段设计与验收报告、本索引和踩坑说明，核对测试数量、命令、哈希与文件保护结果。

## 四、实际执行的命令与结果

| 命令 | 实际结果 |
|---|---|
| `python -m unittest discover -s tests -v`（第二阶段基线） | `Ran 97 tests in 0.680s`，`OK`；见 `reports/stage3_baseline_tests.txt` |
| 第三阶段修复前定向测试 | `Ran 60 tests in 0.966s`，`FAILED (failures=4, errors=2)`；见 `reports/stage3_pre_fix_tests.txt` |
| `python -m unittest discover -s tests -v`（第三阶段最终） | `Ran 121 tests in 1.058s`，`OK` |
| `python scripts/run_stage4_acceptance.py`（第四阶段） | 退出码 0；121 个测试通过；独立对账差异 0；原始文件哈希一致；见 `evidence/stage4/20261022_233849/` |
| `python -m compileall -q main.py web_app.py src tests scripts`（第五阶段） | 退出码 0 |
| `python -m unittest discover -s tests -v`（第五阶段最终） | `Ran 174 tests in 4.241s`，`OK`；见 `reports/stage5_final_tests.txt` |
| `python main.py`（第五阶段） | 退出码 0；120/115/5/5/110；合规 47 / 待确认 56 / 违规 7；见 `reports/stage5_cli_run.txt` |
| `python main.py --html-output reports/compliance_report.html --csv-output reports/compliance_details.csv` | 退出码 0；Markdown/HTML/CSV 均生成；见 `reports/stage5_html_generation.txt` |
| `python scripts/check_stage5_cross_entry.py --output reports/stage5_cross_entry_check.txt` | 退出码 0；三入口各 120 行逐字段一致；见 `reports/stage5_cross_entry_check.txt` |
| `python scripts/run_stage5_web_smoke.py --port 5000` | 退出码 0；任务 `ab3fb8aaa883032bdc7e3910114d8670`；全部 HTTP 检查通过并删除；见 `reports/stage5_web_smoke.json` / `.txt` |
| `python scripts/run_stage5_browser_smoke.py --port 5000` | 退出码 0；任务 `06bd9b0fb56be6406fe9b6c96658e10c`、`e22df3e102fc8e38d623e9991a23c5c9`；桌面/390px 通过；见 `reports/stage5_browser_smoke.json` / `.txt` |

第五阶段关键结果：

- 全量测试 174 个，原有 121 个全部保留；
- Markdown/HTML/CSV 120 行逐字段一致；
- Web Markdown/CSV 与 CLI 逐字节一致，Web HTML 120 行一致；
- 浏览器默认 20 条/页、分页 2/6、窄屏溢出 0px、外部请求 0、控制台错误 0；
- 原始五个文件 SHA-256 与审计基线一致，无数据库 sidecar；
- 第四阶段 `SHA256SUMS` 全部校验通过。

## 五、产出与代码的对应关系

| 产出 | 对应代码 |
|---|---|
| SKU 映射与缺失保留 | `processor.map_sku_ref`、`processor.process_sku_inputs` |
| 已下架跳过规则/价格 | `processor.process_sku_inputs` 分支 |
| 三条件规则匹配 | `rules_engine.match_rules`、`evaluate_product` |
| 多规则取最严 | `rules_engine.evaluate_risk` |
| 人民币换算 | `pricing.convert_to_cny` |
| 完整性五数 | `validation.compute_stats`、`validate_processing_results` |
| Markdown 明细/汇总/自检 | `report.build_detail_section`、`build_summary_section`、`build_integrity_section`、`verify_report_file` |
| 统一检查服务 | `application_service.run_compliance_check`、`ComplianceRun` |
| API/任务快照 JSON | `application_service.build_result_payload` |
| CSV 导出 | `application_service.build_details_csv`、`write_details_csv` |
| HTML 报告 | `html_report.generate_html_report` |
| Web 路由/上传/错误 | `web_app.create_app`、`api_create_report` 等 |
| 任务 ID/路径防护/清理 | `job_store.JobStore` |
| 轮转日志 | `logging_config.configure_logging`、`task_logger` |
| 前端上传/图表/筛选/分页 | `static/js/app.js`、`templates/_dashboard.html` |
| 命令行运行 | `main.run`、`main.main` |
| 独立逐 SKU 对账 | `tests/test_independent_reconciliation.py` |
| 第五阶段 HTTP 冒烟 | `scripts/run_stage5_web_smoke.py` |
| 第五阶段浏览器冒烟 | `scripts/run_stage5_browser_smoke.py` |
| 第五阶段跨入口核对 | `scripts/check_stage5_cross_entry.py` |
| 第四阶段一键验收 | `scripts/run_stage4_acceptance.py` |
| 验收证据包 | `evidence/stage4/20261022_233849/`、`reports/stage5_*`、`reports/screenshots/` |
| 审查缺陷与修复证据 | `docs/第三阶段代码审查与修复报告.md` |
| 第四阶段验收报告 | `docs/第四阶段运行验收与证据报告.md` |
| 第五阶段设计与验收报告 | `docs/第五阶段产品化设计与验收报告.md` |

## 六、说明

- 本索引只记录实际做过的工作，没有把“计划”写成“已完成”。
- 第五阶段测试结果、CLI 输出、HTTP/浏览器冒烟和跨入口核对分别保存在 `reports/stage5_*` 与 `reports/screenshots/`。
- Playwright 与 Chrome 仅用于可选浏览器验收，不是 Web 应用运行依赖；未写入 `requirements.txt`。
- 真实会话原始 JSONL 已随提交压缩包放在 `ai_records/`，并附 `ai_records/README.md` 索引；公开仓库不包含该目录。本文件仍作为可核对的索引。
