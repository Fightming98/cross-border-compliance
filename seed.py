"""种子代码：合规检查助手示例起点。

由 AI 辅助生成，未做人工校对——能跑，但质量请自行判断。
用法：python3 seed.py
"""

import csv
import sqlite3

DB = "compliance.db"
LIST_FILE = "sku_list.csv"


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    report = []

    with open(LIST_FILE, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                sku = con.execute(
                    "SELECT * FROM sku_info WHERE sku_id = ?", (row["sku_ref"],)
                ).fetchone()

                # TODO: 按 compliance_rules 匹配规则，得出风险标记
                # TODO: 按 exchange_rates 将价格换算为人民币
                # TODO: 处理库中缺失的商品

                report.append(
                    {
                        "sku_ref": row["sku_ref"],
                        "name": sku["name"] if sku else "(未找到)",
                        "risk_level": "待补充",
                    }
                )
            except Exception:
                pass  # 单条失败不影响整体

    con.close()
    print(f"处理完成，共 {len(report)} 条")


if __name__ == "__main__":
    main()
