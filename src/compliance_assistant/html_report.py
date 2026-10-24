"""独立、离线可用的单文件 HTML 报告。

报告只消费 :class:`ComplianceRun` 中已经算好的结果，不重新匹配规则或
换算价格。所有来自 CSV / SQLite 的文本都经过 HTML 转义，不向 JavaScript
注入任何数据；文件不引用外部样式、字体、图片或图表服务。
"""

from __future__ import annotations

import html
import os
from datetime import datetime
from pathlib import Path

from .application_service import (
    MISSING_PLACEHOLDER,
    NOT_APPLICABLE_PLACEHOLDER,
    ComplianceRun,
    REPORT_VERSION,
)
from .errors import ReportError
from .models import Disposition, ProcessingResult, RiskLevel

RISK_ORDER = (RiskLevel.COMPLIANT, RiskLevel.PENDING, RiskLevel.VIOLATION)
RISK_CLASS = {
    RiskLevel.COMPLIANT: "compliant",
    RiskLevel.PENDING: "pending",
    RiskLevel.VIOLATION: "violation",
}
RISK_ICON = {
    RiskLevel.COMPLIANT: (
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5"/>'
        "</svg>"
    ),
    RiskLevel.PENDING: (
        '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/>'
        '<path d="M12 7v6l4 2"/></svg>'
    ),
    RiskLevel.VIOLATION: (
        '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2.8 20h18.4L12 '
        '3Z"/><path d="M12 9v5"/><path d="M12 17h.01"/></svg>'
    ),
}
DISPOSITION_CLASS = {
    Disposition.NORMAL: "normal",
    Disposition.DELETED: "deleted",
    Disposition.MISSING: "missing",
}


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _pct(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return part / whole * 100.0


def _pct_text(part: int, whole: int) -> str:
    return f"{_pct(part, whole):.1f}%"


def _width(percent: float) -> str:
    return f"{max(0.0, min(100.0, percent)):.2f}%"


def _generated_text(moment: datetime) -> str:
    offset = moment.strftime("%z")
    if offset:
        offset = f"{offset[:3]}:{offset[3:]}"
        return moment.strftime("%Y-%m-%d %H:%M:%S") + f" UTC{offset}"
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _risk_badge(risk: RiskLevel | None) -> str:
    if risk is None:
        return f'<span class="placeholder">{MISSING_PLACEHOLDER}</span>'
    css = RISK_CLASS[risk]
    return (
        f'<span class="status status-{css}">{RISK_ICON[risk]}'
        f"<span>{_e(risk.value)}</span></span>"
    )


def _disposition_badge(disposition: Disposition) -> str:
    return (
        f'<span class="disposition disposition-{DISPOSITION_CLASS[disposition]}">'
        f"{_e(disposition.value)}</span>"
    )


def _detail_cells(result: ProcessingResult) -> tuple[str, ...]:
    product = result.product
    sku_ref = _e(result.sku_input.sku_ref)
    batch_no = _e(result.sku_input.batch_no)
    if result.disposition == Disposition.MISSING:
        product_cells = tuple(_e(MISSING_PLACEHOLDER) for _ in range(4))
        original_price = _e(MISSING_PLACEHOLDER)
        cny_price = _e(MISSING_PLACEHOLDER)
        risk = _e(MISSING_PLACEHOLDER)
        rules = _e(MISSING_PLACEHOLDER)
    elif result.disposition == Disposition.DELETED:
        assert product is not None
        product_cells = (
            _e(product.name),
            _e(product.category),
            _e(product.material),
            _e(product.origin),
        )
        original_price = f"{_e(f'{product.price:.2f}')} {_e(product.currency)}"
        cny_price = _e(NOT_APPLICABLE_PLACEHOLDER)
        risk = _e(NOT_APPLICABLE_PLACEHOLDER)
        rules = _e(NOT_APPLICABLE_PLACEHOLDER)
    else:
        assert product is not None
        product_cells = (
            _e(product.name),
            _e(product.category),
            _e(product.material),
            _e(product.origin),
        )
        original_price = f"{_e(f'{product.price:.2f}')} {_e(product.currency)}"
        cny_price = (
            _e(f"{result.cny_price:.2f}")
            if result.cny_price is not None
            else _e(MISSING_PLACEHOLDER)
        )
        risk = _risk_badge(result.risk)
        rules = _e(";".join(result.matched_rule_ids) or MISSING_PLACEHOLDER)
    return (
        sku_ref,
        batch_no,
        _disposition_badge(result.disposition),
        *product_cells,
        original_price,
        cny_price,
        risk,
        rules,
    )


def _risk_chart(run: ComplianceRun) -> str:
    total = run.stats.n_evaluated
    rows = []
    for level in RISK_ORDER:
        count = run.risk_counts[level]
        css = RISK_CLASS[level]
        rows.append(
            f'<div class="chart-row">'
            f'<div class="chart-label">{RISK_ICON[level]}'
            f"<span>{_e(level.value)}</span></div>"
            f'<div class="chart-track"><span class="chart-fill fill-{css}" '
            f'style="width:{_width(_pct(count, total))}"></span></div>'
            f'<div class="chart-value"><strong>{count}</strong>'
            f"<span>{_pct_text(count, total)}</span></div></div>"
        )
    legend = "".join(
        f'<span class="legend-item">{RISK_ICON[level]}'
        f"{_e(level.value)}</span>"
        for level in RISK_ORDER
    )
    table_rows = "".join(
        f"<tr><td>{_e(level.value)}</td><td>{run.risk_counts[level]}</td>"
        f"<td>{_pct_text(run.risk_counts[level], total)}</td></tr>"
        for level in RISK_ORDER
    )
    return (
        '<div class="legend">'
        + legend
        + '</div><div class="chart" role="img" aria-label="风险等级数量分布；'
        '详细数值见下方数据表。">'
        + "".join(rows)
        + '</div><details class="table-view"><summary>查看风险分布数据表</summary>'
        '<table class="data-table compact"><thead><tr><th>风险</th><th>数量</th>'
        f"<th>占实际评估数</th></tr></thead><tbody>{table_rows}</tbody>"
        f'<tfoot><tr><th>合计</th><th>{total}</th><th>100.0%</th></tr></tfoot>'
        "</table></details>"
    )


def _disposition_chart(run: ComplianceRun) -> str:
    total = run.stats.n_input
    rows = []
    for disposition in Disposition:
        count = run.disposition_counts[disposition]
        css = DISPOSITION_CLASS[disposition]
        label = {
            Disposition.NORMAL: "正常评估",
            Disposition.DELETED: "已下架",
            Disposition.MISSING: "库中缺失",
        }[disposition]
        rows.append(
            f'<div class="chart-row">'
            f'<div class="chart-label"><span class="series-swatch swatch-{css}"></span>'
            f"<span>{_e(label)}</span></div>"
            f'<div class="chart-track"><span class="chart-fill fill-{css}" '
            f'style="width:{_width(_pct(count, total))}"></span></div>'
            f'<div class="chart-value"><strong>{count}</strong>'
            f"<span>{_pct_text(count, total)}</span></div></div>"
        )
    table_rows = "".join(
        f"<tr><td>{_e(disposition.value)}</td>"
        f"<td>{run.disposition_counts[disposition]}</td>"
        f"<td>{_pct_text(run.disposition_counts[disposition], total)}</td></tr>"
        for disposition in Disposition
    )
    return (
        '<div class="chart" role="img" aria-label="处置状态数量分布；'
        '详细数值见下方数据表。">'
        + "".join(rows)
        + '</div><details class="table-view"><summary>查看处置分布数据表</summary>'
        '<table class="data-table compact"><thead><tr><th>处置结果</th><th>数量</th>'
        f"<th>占清单读取数</th></tr></thead><tbody>{table_rows}</tbody>"
        f'<tfoot><tr><th>合计</th><th>{total}</th><th>100.0%</th></tr></tfoot>'
        "</table></details>"
    )


def _top_categories_chart(run: ComplianceRun) -> str:
    top = run.top_categories
    if not top:
        return '<p class="empty-state">没有风险类目。</p>'
    maximum = max(item.total for item in top)

    rows = []
    for item in top:
        total_share = _pct(item.total, maximum)
        violation_share = _pct(item.violations, item.total)
        pending_share = _pct(item.pending, item.total)
        segments = []
        if item.violations:
            segments.append(
                f'<span class="segment segment-violation" '
                f'style="width:{_width(violation_share)}"></span>'
            )
        if item.pending:
            segments.append(
                f'<span class="segment segment-pending" '
                f'style="width:{_width(pending_share)}"></span>'
            )
        rows.append(
            '<div class="chart-row chart-row-top">'
            f'<div class="chart-label"><span>{_e(item.category)}</span></div>'
            f'<div class="chart-track"><div class="stack" '
            f'style="width:{_width(total_share)}">'
            + "".join(segments)
            + "</div></div>"
            f'<div class="chart-value"><strong>{item.total}</strong>'
            f'<span>违规 {item.violations} · 待确认 {item.pending}</span></div>'
            "</div>"
        )
    table_rows = "".join(
        f"<tr><td>{rank}</td><td>{_e(item.category)}</td>"
        f"<td>{item.violations}</td><td>{item.pending}</td><td>{item.total}</td></tr>"
        for rank, item in enumerate(top, start=1)
    )
    return (
        '<div class="legend"><span class="legend-item">'
        '<span class="series-swatch swatch-violation"></span>违规</span>'
        '<span class="legend-item"><span class="series-swatch swatch-pending">'
        "</span>待确认</span></div>"
        '<div class="chart" role="img" aria-label="Top 风险类目对比；'
        '条形长度按风险商品总数相对最大值绘制，详细数值见下方数据表。">'
        + "".join(rows)
        + '</div><details class="table-view"><summary>查看 Top 风险类目数据表</summary>'
        '<table class="data-table compact"><thead><tr><th>排名</th><th>类目</th>'
        "<th>违规</th><th>待确认</th><th>风险商品总数</th></tr></thead>"
        f"<tbody>{table_rows}</tbody></table></details>"
    )


def _detail_table(run: ComplianceRun) -> str:
    rows = []
    for index, result in enumerate(run.results, start=1):
        cells = _detail_cells(result)
        rows.append(
            f'<tr class="detail-row" data-sku-ref="{_e(result.sku_input.sku_ref)}">'
            f'<td class="num">{index}</td><td class="mono">{cells[0]}</td>'
            f"<td>{cells[1]}</td><td>{cells[2]}</td><td>{cells[3]}</td>"
            f"<td>{cells[4]}</td><td>{cells[5]}</td><td>{cells[6]}</td>"
            f'<td class="num">{cells[7]}</td><td class="num">{cells[8]}</td>'
            f"<td>{cells[9]}</td><td class=\"mono rules\">{cells[10]}</td></tr>"
        )
    return (
        '<div class="table-scroll"><table class="data-table detail-table">'
        "<caption>共 "
        f"{len(run.results)} 条输入记录，按原始 CSV 顺序逐条列出。</caption>"
        "<thead><tr><th>序号</th><th>SKU</th><th>批次</th><th>处置结果</th>"
        "<th>商品名</th><th>类目</th><th>材质</th><th>产地</th><th>原价</th>"
        "<th>人民币价格</th><th>风险标记</th><th>依据规则</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _integrity_section(run: ComplianceRun) -> str:
    stats = run.stats
    equation_rows = "".join(
        "<tr>"
        f"<td>{_e(name)}</td><td>{_e(expression)}</td>"
        f'<td><span class="verdict {"ok" if holds else "bad"}">'
        f"{'成立' if holds else '不成立'}</span></td></tr>"
        for name, expression, holds in (
            (
                name,
                f"{left} = " + " + ".join(str(term) for term in terms),
                holds,
            )
            for name, left, terms, holds in stats.equation_rows()
        )
    )
    risk_total = sum(run.risk_counts.values())
    risk_holds = risk_total == stats.n_evaluated
    risk_expression = (
        " + ".join(str(run.risk_counts[level]) for level in RISK_ORDER)
        + f" = {risk_total}"
    )
    overall = stats.equations_hold and risk_holds
    return f"""
    <section class="panel" id="integrity">
      <div class="section-heading">
        <div><p class="eyebrow">INTEGRITY CHECK</p><h2>完整性自检</h2></div>
        <span class="verdict {'ok' if overall else 'bad'}">
          {'全部等式成立' if overall else '存在不成立的等式'}
        </span>
      </div>
      <div class="mini-stats">
        <div><span>清单读取数</span><strong>{stats.n_input}</strong></div>
        <div><span>在库数</span><strong>{stats.n_matched}</strong></div>
        <div><span>已下架数</span><strong>{stats.n_deleted}</strong></div>
        <div><span>库中缺失数</span><strong>{stats.n_missing}</strong></div>
        <div><span>实际评估数</span><strong>{stats.n_evaluated}</strong></div>
      </div>
      <table class="data-table compact integrity-table">
        <thead><tr><th>校验项</th><th>等式</th><th>结果</th></tr></thead>
        <tbody>{equation_rows}
          <tr><td>三档风险数量之和 = 实际评估数</td>
          <td>{_e(risk_expression)}</td>
          <td><span class="verdict {'ok' if risk_holds else 'bad'}">
          {'成立' if risk_holds else '不成立'}</span></td></tr>
        </tbody>
      </table>
    </section>
    """


def _stat_cards(run: ComplianceRun) -> str:
    items = [
        ("清单读取数", run.stats.n_input, "input"),
        ("在库商品数", run.stats.n_matched, "matched"),
        ("正常评估数", run.stats.n_evaluated, "evaluated"),
        ("已下架数", run.stats.n_deleted, "deleted"),
        ("库中缺失数", run.stats.n_missing, "missing"),
        ("合规", run.risk_counts[RiskLevel.COMPLIANT], "compliant"),
        ("待确认", run.risk_counts[RiskLevel.PENDING], "pending"),
        ("违规", run.risk_counts[RiskLevel.VIOLATION], "violation"),
    ]
    return "".join(
        f'<div class="stat-card stat-{css}"><span>{_e(label)}</span>'
        f"<strong>{value}</strong></div>"
        for label, value, css in items
    )


def _css() -> str:
    return """
    :root {
      color-scheme: light;
      --page: #f3f6fa;
      --surface: #ffffff;
      --surface-subtle: #f7f9fc;
      --ink: #172033;
      --ink-secondary: #475467;
      --ink-muted: #667085;
      --border: #e2e8f0;
      --grid: #e8edf4;
      --primary: #1d4ed8;
      --primary-soft: #eaf1ff;
      --compliant: #0ca30c;
      --pending: #fab219;
      --violation: #d03b3b;
      --normal: #2a78d6;
      --deleted: #eb6834;
      --missing: #1baf7a;
      --good-text: #006300;
      --critical-text: #b42318;
      --radius: 10px;
      --shadow: 0 1px 2px rgba(16, 24, 40, .04);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        color-scheme: dark;
        --page: #0d0d0d;
        --surface: #1a1a19;
        --surface-subtle: #222220;
        --ink: #f0efec;
        --ink-secondary: #c3c2b7;
        --ink-muted: #94928a;
        --border: #383835;
        --grid: #2c2c2a;
        --primary: #3987e5;
        --primary-soft: #172b46;
        --normal: #3987e5;
        --deleted: #d95926;
        --missing: #199e70;
        --good-text: #0ca30c;
        --critical-text: #ff8d8d;
        --shadow: none;
      }
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      background: var(--page);
      color: var(--ink);
      font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei",
        "PingFang SC", "Noto Sans CJK SC", sans-serif;
      font-size: 14px;
      line-height: 1.65;
    }
    a { color: var(--primary); }
    .report-shell { max-width: 1240px; margin: 0 auto; padding: 28px 24px 56px; }
    .report-header {
      display: flex; justify-content: space-between; gap: 24px;
      align-items: flex-start; padding: 28px 30px;
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); box-shadow: var(--shadow);
    }
    .brand { display: flex; gap: 14px; align-items: center; }
    .brand-mark {
      width: 44px; height: 44px; border-radius: 12px; display: grid;
      place-items: center; background: var(--primary-soft); color: var(--primary);
      flex: 0 0 auto;
    }
    .brand-mark svg { width: 24px; height: 24px; fill: none; stroke: currentColor;
      stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
    h1 { font-size: 24px; line-height: 1.3; margin: 2px 0 4px; letter-spacing: -.02em; }
    h2 { font-size: 18px; margin: 0; letter-spacing: -.01em; }
    .eyebrow {
      margin: 0; color: var(--primary); font-size: 11px; font-weight: 700;
      letter-spacing: .12em;
    }
    .subtitle { margin: 0; color: var(--ink-muted); }
    .header-meta { display: grid; gap: 8px; min-width: 280px; }
    .meta-row { display: flex; justify-content: space-between; gap: 20px; }
    .meta-row span { color: var(--ink-muted); }
    .meta-row strong { font-weight: 600; text-align: right; overflow-wrap: anywhere; }
    .status-pill {
      display: inline-flex; align-items: center; gap: 7px; color: var(--good-text);
      background: color-mix(in srgb, var(--compliant) 12%, var(--surface));
      border-radius: 999px; padding: 4px 10px; font-weight: 650;
    }
    .status-pill::before {
      content: ""; width: 7px; height: 7px; border-radius: 50%;
      background: var(--compliant);
    }
    .stats-grid {
      display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px; margin: 16px 0;
    }
    .stat-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 15px 16px; box-shadow: var(--shadow);
      border-top: 3px solid var(--border);
    }
    .stat-card span { display: block; color: var(--ink-muted); font-size: 12px; }
    .stat-card strong {
      display: block; margin-top: 5px; font-size: 28px; line-height: 1.15;
      font-weight: 680; letter-spacing: -.03em;
    }
    .stat-compliant { border-top-color: var(--compliant); }
    .stat-pending { border-top-color: var(--pending); }
    .stat-violation { border-top-color: var(--violation); }
    .stat-evaluated { border-top-color: var(--normal); }
    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .panel {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 20px 22px; box-shadow: var(--shadow);
      margin-bottom: 16px;
    }
    .section-heading {
      display: flex; align-items: flex-start; justify-content: space-between;
      gap: 20px; margin-bottom: 18px;
    }
    .section-heading p { margin: 0 0 2px; }
    .legend { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-bottom: 12px; }
    .legend-item { display: inline-flex; align-items: center; gap: 6px;
      color: var(--ink-secondary); font-size: 12px; }
    .series-swatch { width: 9px; height: 9px; border-radius: 2px; display: inline-block; }
    .swatch-normal { background: var(--normal); }
    .swatch-deleted { background: var(--deleted); }
    .swatch-missing { background: var(--missing); }
    .swatch-pending, .segment-pending { background: var(--pending); }
    .swatch-violation, .segment-violation { background: var(--violation); }
    .chart { display: grid; gap: 11px; }
    .chart-row {
      display: grid; grid-template-columns: 112px minmax(0, 1fr) 136px;
      align-items: center; gap: 12px;
    }
    .chart-label { display: flex; align-items: center; gap: 7px; min-width: 0; }
    .chart-label svg { width: 15px; height: 15px; flex: 0 0 auto; fill: none;
      stroke: currentColor; stroke-width: 2; stroke-linecap: round;
      stroke-linejoin: round; color: var(--ink-secondary); }
    .chart-label span { color: var(--ink-secondary); font-size: 12px; }
    .chart-track {
      height: 10px; border-radius: 2px; background: var(--grid);
      overflow: hidden;
    }
    .chart-fill {
      display: block; height: 100%; min-width: 0;
      border-radius: 0 4px 4px 0;
    }
    .fill-compliant { background: var(--compliant); }
    .fill-pending { background: var(--pending); }
    .fill-violation { background: var(--violation); }
    .fill-normal { background: var(--normal); }
    .fill-deleted { background: var(--deleted); }
    .fill-missing { background: var(--missing); }
    .chart-value {
      display: flex; align-items: baseline; justify-content: flex-end;
      gap: 7px; font-variant-numeric: tabular-nums; white-space: nowrap;
    }
    .chart-value strong { font-size: 14px; }
    .chart-value span { color: var(--ink-muted); font-size: 11px; }
    .chart-row-top .chart-track { height: 12px; background: transparent; overflow: visible; }
    .stack { display: flex; gap: 2px; height: 12px; min-width: 2px; }
    .segment { display: block; height: 100%; min-width: 1px; }
    .segment:first-child { border-radius: 0; }
    .segment:last-child { border-radius: 0 4px 4px 0; }
    .table-view { margin-top: 14px; }
    .table-view summary { cursor: pointer; color: var(--primary); font-size: 12px;
      font-weight: 600; }
    .table-scroll { overflow-x: auto; border: 1px solid var(--border);
      border-radius: 8px; }
    .data-table { width: 100%; border-collapse: collapse; }
    .data-table caption {
      text-align: left; color: var(--ink-muted); padding: 9px 12px;
      border-bottom: 1px solid var(--border); background: var(--surface-subtle);
      font-size: 12px;
    }
    .data-table th, .data-table td {
      padding: 9px 11px; border-bottom: 1px solid var(--grid);
      text-align: left; vertical-align: top;
    }
    .data-table th {
      background: var(--surface-subtle); color: var(--ink-secondary);
      font-size: 11px; font-weight: 650; white-space: nowrap;
    }
    .data-table tbody tr:last-child td { border-bottom: 0; }
    .data-table.compact { margin-top: 8px; font-size: 12px; }
    .data-table.compact th, .data-table.compact td { padding: 7px 10px; }
    .data-table tfoot th { border-top: 1px solid var(--border); }
    .detail-table { min-width: 1120px; font-size: 12px; }
    .detail-table td { color: var(--ink-secondary); }
    .detail-table td.num, .detail-table th.num { text-align: right;
      font-variant-numeric: tabular-nums; white-space: nowrap; }
    .mono { font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 11.5px; }
    .rules { overflow-wrap: anywhere; min-width: 100px; }
    .placeholder { color: var(--ink-muted); }
    .status, .disposition {
      display: inline-flex; align-items: center; gap: 4px; border-radius: 999px;
      padding: 2px 8px; font-size: 11px; font-weight: 650; white-space: nowrap;
    }
    .status svg { width: 12px; height: 12px; fill: none; stroke: currentColor;
      stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }
    .status-compliant { color: var(--good-text);
      background: color-mix(in srgb, var(--compliant) 12%, var(--surface)); }
    .status-pending { color: #7a4b00;
      background: color-mix(in srgb, var(--pending) 20%, var(--surface)); }
    .status-violation { color: var(--critical-text);
      background: color-mix(in srgb, var(--violation) 12%, var(--surface)); }
    .disposition-normal { color: var(--normal);
      background: color-mix(in srgb, var(--normal) 10%, var(--surface)); }
    .disposition-deleted { color: var(--deleted);
      background: color-mix(in srgb, var(--deleted) 10%, var(--surface)); }
    .disposition-missing { color: var(--missing);
      background: color-mix(in srgb, var(--missing) 10%, var(--surface)); }
    .verdict { font-weight: 650; white-space: nowrap; }
    .verdict.ok { color: var(--good-text); }
    .verdict.bad { color: var(--critical-text); }
    .mini-stats { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px; margin-bottom: 12px; }
    .mini-stats div { background: var(--surface-subtle); border-radius: 8px;
      padding: 10px 12px; }
    .mini-stats span { display: block; color: var(--ink-muted); font-size: 11px; }
    .mini-stats strong { display: block; margin-top: 2px; font-size: 20px; }
    .empty-state { color: var(--ink-muted); margin: 0; padding: 12px 0; }
    .report-footer {
      margin-top: 20px; color: var(--ink-muted); font-size: 12px;
      border-top: 1px solid var(--border); padding-top: 16px;
    }
    .report-footer p { margin: 4px 0; }
    @media (max-width: 900px) {
      .report-shell { padding: 16px 14px 40px; }
      .report-header { flex-direction: column; padding: 20px; }
      .header-meta { width: 100%; min-width: 0; }
      .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .grid-2 { grid-template-columns: 1fr; }
      .mini-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 620px) {
      h1 { font-size: 20px; }
      .chart-row { grid-template-columns: 92px minmax(0, 1fr); gap: 9px; }
      .chart-value { grid-column: 2; justify-content: flex-start; }
      .stats-grid { gap: 9px; }
      .stat-card strong { font-size: 24px; }
      .panel { padding: 16px; }
    }
    @media print {
      body { background: #fff; }
      .report-shell { max-width: none; padding: 0; }
      .panel, .report-header, .stat-card { box-shadow: none; break-inside: avoid; }
      .table-view { display: none; }
      .table-scroll { overflow: visible; }
      .detail-table { min-width: 0; font-size: 9px; }
    }
    """


def build_html_report(
    run: ComplianceRun,
    *,
    report_id: str | None = None,
) -> str:
    """构造完整单文件 HTML 文本。"""

    identifier = report_id or (
        f"CLI-{run.generated_at.strftime('%Y%m%d-%H%M%S')}-"
        f"{run.source_sha256[:8].upper()}"
    )
    risk_pct = f"{run.risk_ratio * 100:.1f}%"
    title = "跨境商品合规检查报告"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{_e(title)} · {_e(identifier)}</title>
<style>{_css()}</style>
</head>
<body>
<main class="report-shell">
  <header class="report-header">
    <div>
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M12 3 4 6v6c0 4.5 3.2 7.8 8 9 4.8-1.2 8-4.5 8-9V6l-8-3Z"/><path d="m8.5 12 2.2 2.2 4.8-4.8"/></svg>
        </span>
        <div>
          <p class="eyebrow">CROSS-BORDER COMPLIANCE</p>
          <h1>{_e(title)}</h1>
          <p class="subtitle">离线单文件报告 · 数据来自统一检查结果</p>
        </div>
      </div>
    </div>
    <div class="header-meta">
      <div class="meta-row"><span>任务编号</span><strong class="mono">{_e(identifier)}</strong></div>
      <div class="meta-row"><span>生成时间</span><strong>{_e(_generated_text(run.generated_at))}</strong></div>
      <div class="meta-row"><span>输入文件</span><strong>{_e(run.source_filename)}</strong></div>
      <div class="meta-row"><span>输入 SHA-256</span><strong class="mono">{_e(run.source_sha256)}</strong></div>
      <div class="meta-row"><span>检查状态</span><span class="status-pill">已完成</span></div>
      <div class="meta-row"><span>报告版本</span><strong>{_e(REPORT_VERSION)}</strong></div>
    </div>
  </header>

  <section class="stats-grid" aria-label="检查概览">
    {_stat_cards(run)}
  </section>

  <div class="grid-2">
    <section class="panel">
      <div class="section-heading">
        <div><p class="eyebrow">RISK DISTRIBUTION</p><h2>风险等级分布</h2></div>
        <span class="verdict ok">风险商品占比 {_e(risk_pct)}</span>
      </div>
      <p class="subtitle">仅统计正常评估商品；风险商品 = 待确认 + 违规。</p>
      {_risk_chart(run)}
    </section>
    <section class="panel">
      <div class="section-heading">
        <div><p class="eyebrow">DISPOSITION</p><h2>处置状态分布</h2></div>
        <span class="subtitle">共 {run.stats.n_input} 条</span>
      </div>
      <p class="subtitle">已下架与库中缺失不进入规则评估，也不标记为合规。</p>
      {_disposition_chart(run)}
    </section>
  </div>

  <section class="panel" id="risk-categories">
    <div class="section-heading">
      <div><p class="eyebrow">TOP RISK CATEGORIES</p><h2>Top 风险类目</h2></div>
      <span class="subtitle">最多 5 个</span>
    </div>
    <p class="subtitle">排序：违规数降序 → 待确认数降序 → 类目名升序。</p>
    {_top_categories_chart(run)}
  </section>

  <section class="panel" id="details">
    <div class="section-heading">
      <div><p class="eyebrow">SKU DETAILS</p><h2>商品明细</h2></div>
      <span class="subtitle">共 {len(run.results)} 条</span>
    </div>
    {_detail_table(run)}
  </section>

  {_integrity_section(run)}

  <footer class="report-footer">
    <p><strong>口径说明：</strong>风险等级为“合规 / 待确认 / 违规”；处置状态为“正常评估 / 已下架 / 库中缺失”。</p>
    <p>人民币价格 = 商品原价 × 数据库汇率，使用 Decimal 与 ROUND_HALF_UP 保留两位小数；缺失与已下架商品不参与价格换算。</p>
    <p>“在库数”包含已下架商品；库中缺失商品保留在明细中，不使用占位值伪装为“合规”。</p>
    <p>本报告为本地离线文件，不引用外部 CDN、远程字体、网络图片或在线图表服务。</p>
  </footer>
</main>
</body>
</html>
"""


def _verify_html_document(text: str, expected_rows: int) -> None:
    required = [
        "<!doctype html>",
        "跨境商品合规检查报告",
        "风险等级分布",
        "处置状态分布",
        "Top 风险类目",
        "商品明细",
        "完整性自检",
    ]
    missing = [marker for marker in required if marker not in text]
    if missing:
        raise ReportError(f"HTML 报告缺少必需内容: {missing}")
    actual_rows = text.count('class="detail-row"')
    if actual_rows != expected_rows:
        raise ReportError(
            f"HTML 报告明细行数应为 {expected_rows}，实际为 {actual_rows}"
        )
    lowered = text.lower()
    for forbidden in ("<script src=", "<link rel=\"stylesheet\"", "http://", "https://"):
        if forbidden in lowered:
            raise ReportError(
                f"HTML 报告包含外部资源引用，不能离线独立打开: {forbidden}"
            )


def generate_html_report(
    run: ComplianceRun,
    output_path: str | Path,
    *,
    report_id: str | None = None,
) -> Path:
    """生成、原子写入并复核单文件 HTML 报告。"""

    content = build_html_report(run, report_id=report_id)
    try:
        _verify_html_document(content, len(run.results))
    except ReportError:
        raise

    path = Path(output_path)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp_path, path)
    except OSError as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise ReportError(f"写入 HTML 报告失败: {path}: {exc}") from exc

    try:
        saved = path.read_text(encoding="utf-8")
        _verify_html_document(saved, len(run.results))
    except (OSError, UnicodeDecodeError, ReportError) as exc:
        try:
            path.unlink()
        except OSError:
            pass
        if isinstance(exc, ReportError):
            raise
        raise ReportError(f"HTML 报告落盘后不可读: {path}: {exc}") from exc
    return path
