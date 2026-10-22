# 第四阶段验收摘要（自动生成）

> 运行标识：`20261022_233708`
> 生成时间：2026-10-22T23:37:11+08:00
> 项目根目录：`E:\Desktop\AI全栈工程师校招笔试-材料包`

## 命令结果

| 步骤 | 命令 | 退出码 | 结果 | 日志 |
|---|---|---:|---|---|
| compile | `D:\python\Anaconda\python.exe -m compileall -q main.py src tests` | 0 | 通过 | evidence/stage4/20261022_233708/01_compile.log |
| unit_tests | `D:\python\Anaconda\python.exe -m unittest discover -s tests -v` | 0 | 通过 | evidence/stage4/20261022_233708/02_unit_tests.log |
| independent_reconciliation | `D:\python\Anaconda\python.exe -m unittest -v tests.test_independent_reconciliation` | 0 | 通过 | evidence/stage4/20261022_233708/03_independent_reconciliation.log |
| main_run | `D:\python\Anaconda\python.exe main.py` | 0 | 通过 | evidence/stage4/20261022_233708/04_main_run.log |

## 独立复算

- 清单读取数：120
- 在库数（含已下架）：115
- 已下架数：5
- 库中缺失数：5
- 实际评估数：110
- 风险统计：{'合规': 47, '待确认': 56, '违规': 7}
- 测试总数：121；独立对账测试数：4

## Top 风险类目

| 排名 | 类目 | 违规 | 待确认 | 风险商品总数 |
|---:|---|---:|---:|---:|
| 1 | 内衣 | 4 | 6 | 10 |
| 2 | 箱包 | 3 | 18 | 21 |
| 3 | 配饰 | 0 | 13 | 13 |
| 4 | 鞋靴 | 0 | 11 | 11 |
| 5 | 上衣 | 0 | 5 | 5 |

## 检查结论

- 处置等式：True
- 逐 SKU 对账：True
- 价格复算：True
- Top 类目：True
- Markdown 结构：True
- 原始文件哈希：True
- 审计文件哈希：True
- **综合结论：通过**

详细检查见 `05_integrity_checks.log`；机器可读结果见 `acceptance_summary.json`。
