"""Interactive Plotly figures.

`build_charts()` is the single place a chart is defined; the CLI writes the figures
to standalone HTML and the Streamlit app renders the same objects with
`st.plotly_chart`. Figures are keyed by slug with no file extension, because the same
figure is now saved as HTML rather than PNG.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .domain import CLASS_COLUMNS

GAIN = "#2ca02c"
LOSS = "#c0392b"
PRIMARY = "#1f4e79"
MUTED = "#7f7f7f"
CLASS_PALETTE = [
    "#4c78a8",
    "#f58518",
    "#54a24b",
    "#e45756",
    "#72b7b2",
    "#ff9da6",
    "#9d755d",
    "#bab0ac",
]

MONEY = "$,.0f"
PERCENT = ".2%"


def layout(fig: go.Figure, title: str, *, height: int = 460, y_title: str = None,
           tick_format: str = None, rotate: bool = True) -> go.Figure:
    fig.update_layout(
        title=title,
        height=height,
        margin=dict(l=70, r=40, t=60, b=80),
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    fig.update_yaxes(title_text=y_title, tickformat=tick_format, gridcolor="rgba(0,0,0,0.08)")
    fig.update_xaxes(tickangle=-45 if rotate else 0, gridcolor="rgba(0,0,0,0.05)")
    return fig


def unique_labels(series) -> list:
    counts = {}
    out = []
    for label in series:
        counts[label] = counts.get(label, 0) + 1
        out.append(label if counts[label] == 1 else f"{label} #{counts[label]}")
    return out


def build_charts(frame: pd.DataFrame, holdings: pd.DataFrame, metrics: dict):
    """Build the chart set and return [(slug, figure)] without saving."""
    figures = []
    months = frame["month"].tolist()
    single = len(frame) == 1

    # 1. account value and cost basis
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=months, y=frame["ending_value"], name="Account value", mode="lines+markers",
        line=dict(color=PRIMARY, width=2.5), hovertemplate="%{x}<br>$%{y:,.2f}<extra>Account value</extra>",
    ))
    if frame["cost_basis"].notna().any():
        fig.add_trace(go.Scatter(
            x=months, y=frame["cost_basis"], name="Cost basis (positions)", mode="lines+markers",
            line=dict(color=MUTED, width=1.5, dash="dash"),
            hovertemplate="%{x}<br>$%{y:,.2f}<extra>Cost basis</extra>",
        ))
    contributions = frame[frame["net_flow"].abs() > 0.01]
    if not contributions.empty:
        fig.add_trace(go.Scatter(
            x=contributions["month"], y=contributions["ending_value"], name="Net external flow",
            mode="markers", marker=dict(color=GAIN, size=12, symbol="circle-open", line=dict(width=3)),
            customdata=contributions["net_flow"],
            hovertemplate="%{x}<br>flow $%{customdata:,.2f}<extra>External flow</extra>",
        ))
    figures.append(("01_account_value", layout(
        fig, "Account Value by Statement Period", y_title="Value", tick_format=MONEY)))

    # 2. monthly return bars
    values = frame["twr"].fillna(0.0)
    fig = go.Figure(go.Bar(
        x=months, y=values, marker_color=[GAIN if v >= 0 else LOSS for v in values],
        text=[f"{v * 100:.2f}%" for v in values],
        textposition="outside" if len(frame) <= 18 else "none",
        hovertemplate="%{x}<br>%{y:.2%}<extra>TWR</extra>",
    ))
    fig.add_hline(y=0, line_width=1, line_color="black")
    figures.append(("02_monthly_returns", layout(
        fig, "Monthly Time-Weighted Return (Modified Dietz)", tick_format=PERCENT)))

    # 3. cumulative index + drawdown
    if not single:
        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True, row_heights=[0.66, 0.34], vertical_spacing=0.09,
            subplot_titles=("Cumulative Time-Weighted Return Index (start = 100)",
                            "Drawdown from Peak (return index)"),
        )
        fig.add_trace(go.Scatter(
            x=months, y=frame["twr_index"], name="TWR index", mode="lines+markers",
            line=dict(color=PRIMARY, width=2.5),
            hovertemplate="%{x}<br>%{y:.2f}<extra>Index</extra>",
        ), row=1, col=1)
        fig.add_hline(y=100, line_dash="dash", line_color="gray", row=1, col=1)
        fig.add_trace(go.Scatter(
            x=months, y=frame["drawdown"], name="Drawdown", fill="tozeroy", mode="lines",
            line=dict(color=LOSS, width=1), fillcolor="rgba(192,57,43,0.35)",
            hovertemplate="%{x}<br>%{y:.2%}<extra>Drawdown</extra>",
        ), row=2, col=1)
        fig.update_yaxes(title_text="Index", row=1, col=1)
        fig.update_yaxes(tickformat=PERCENT, row=2, col=1)
        figures.append(("03_cumulative_and_drawdown", layout(fig, "", height=640)))

    # 4. allocation weights over time + latest mix
    weight_columns = [
        f"weight_{name}" for name in CLASS_COLUMNS if frame[f"weight_{name}"].notna().any()
    ]
    if weight_columns:
        fig = make_subplots(
            rows=1, cols=2, column_widths=[0.58, 0.42],
            specs=[[{"type": "xy"}, {"type": "domain"}]],
            subplot_titles=("Asset Allocation Weights", "Allocation of Long Assets"),
        )
        data = frame[weight_columns].fillna(0.0)
        for index, column in enumerate(weight_columns):
            name = column.replace("weight_", "")
            fig.add_trace(go.Scatter(
                x=months, y=data[column], name=name, mode="lines",
                stackgroup="weights", line=dict(width=0.5),
                fillcolor=CLASS_PALETTE[index % len(CLASS_PALETTE)],
                hovertemplate="%{x}<br>%{y:.2%}<extra>" + name + "</extra>",
            ), row=1, col=1)

        latest = frame.iloc[-1]
        labels, sizes = [], []
        for name in CLASS_COLUMNS:
            value = latest.get(f"alloc_{name}")
            if value is not None and pd.notna(value) and value > 0:
                labels.append(name)
                sizes.append(value)
        if sizes:
            fig.add_trace(go.Pie(
                labels=labels, values=sizes, hole=0.42, sort=False,
                marker=dict(colors=CLASS_PALETTE[: len(sizes)]),
                texttemplate="%{label}<br>%{percent}",
                hovertemplate="%{label}<br>$%{value:,.2f} (%{percent})<extra></extra>",
            ), row=1, col=2)
        fig.update_yaxes(tickformat=PERCENT, row=1, col=1)
        figures.append(("04_asset_allocation", layout(
            fig, f"Asset Allocation — long assets as of {latest['month']}", height=500)))

    # 5. income
    if "income_total" in frame and frame["income_total"].notna().any():
        dividends = frame.get("dividends", pd.Series(0.0, index=frame.index)).fillna(0.0)
        interest = frame.get("interest", pd.Series(0.0, index=frame.index)).fillna(0.0)
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Bar(
            x=months, y=dividends, name="Dividends", marker_color="#1f77b4",
            hovertemplate="%{x}<br>$%{y:,.2f}<extra>Dividends</extra>",
        ), secondary_y=False)
        fig.add_trace(go.Bar(
            x=months, y=interest, name="Interest", marker_color="#ff7f0e",
            hovertemplate="%{x}<br>$%{y:,.2f}<extra>Interest</extra>",
        ), secondary_y=False)
        fig.add_trace(go.Scatter(
            x=months, y=(dividends + interest).cumsum(), name="Cumulative", mode="lines+markers",
            line=dict(color="#333333", width=2),
            hovertemplate="%{x}<br>$%{y:,.2f}<extra>Cumulative</extra>",
        ), secondary_y=True)
        fig.update_layout(barmode="stack")
        fig.update_yaxes(title_text="Income", tickformat=MONEY, secondary_y=False)
        fig.update_yaxes(title_text="Cumulative", tickformat=MONEY, secondary_y=True,
                         showgrid=False)
        figures.append(("05_income", layout(fig, "Monthly Income")))

    # 6. realized vs unrealized
    if "st_net" in frame and frame["st_net"].notna().any():
        short = frame["st_net"].fillna(0.0)
        long_term = frame.get("lt_net", pd.Series(0.0, index=frame.index)).fillna(0.0)
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=months, y=short, name="Realized short-term", marker_color="#8e44ad",
            hovertemplate="%{x}<br>$%{y:,.2f}<extra>Realized ST</extra>",
        ))
        fig.add_trace(go.Bar(
            x=months, y=long_term, name="Realized long-term", marker_color="#16a085",
            hovertemplate="%{x}<br>$%{y:,.2f}<extra>Realized LT</extra>",
        ))
        if "unrealized" in frame and frame["unrealized"].notna().any():
            fig.add_trace(go.Scatter(
                x=months, y=frame["unrealized"], name="Unrealized (period end)",
                mode="lines+markers", line=dict(color="#d35400", width=2.5),
                hovertemplate="%{x}<br>$%{y:,.2f}<extra>Unrealized</extra>",
            ))
        fig.add_hline(y=0, line_width=1, line_color="black")
        fig.update_layout(barmode="group")
        figures.append(("06_realized_unrealized", layout(
            fig, "Realized Gains by Tax Character vs Unrealized Position", tick_format=MONEY)))

    # 7. value reconciliation waterfall
    steps = [
        ("Deposits", float(frame["deposits"].fillna(0.0).sum())),
        ("Withdrawals", float(frame["withdrawals"].fillna(0.0).sum())),
        ("Market appr.", float(frame["market_appreciation"].fillna(0.0).sum())),
        ("Income", float(frame["dividends_interest"].fillna(0.0).sum())),
        ("Expenses", float(frame["expenses"].fillna(0.0).sum())),
    ]
    start_value = float(frame["beginning_value"].iloc[0])
    ending = start_value + sum(amount for _, amount in steps)
    fig = go.Figure(go.Waterfall(
        orientation="v",
        measure=["absolute"] + ["relative"] * len(steps) + ["total"],
        x=["Beginning"] + [label for label, _ in steps] + ["Ending"],
        y=[start_value] + [amount for _, amount in steps] + [None],
        text=[f"${start_value:,.0f}"] + [f"{amount:+,.0f}" for _, amount in steps]
             + [f"${ending:,.0f}"],
        textposition="outside",
        increasing=dict(marker_color=GAIN),
        decreasing=dict(marker_color=LOSS),
        totals=dict(marker_color=PRIMARY),
        connector=dict(line=dict(color="rgba(0,0,0,0.25)")),
        hovertemplate="%{x}<br>$%{y:,.2f}<extra></extra>",
    ))
    figures.append(("07_value_reconciliation", layout(
        fig, "Value Reconciliation: Beginning to Ending", tick_format=MONEY, rotate=False)))

    # 8/9. latest holdings
    if not holdings.empty:
        latest_month = holdings["month"].max()
        current = holdings[holdings["month"] == latest_month].copy()
        current["chart_label"] = unique_labels(
            current["label"].fillna(current["symbol"]) + " [" + current["asset_class"].str[:3] + "]"
        )

        priced = current[current["unrealized"].notna()].sort_values("unrealized")
        if not priced.empty:
            fig = go.Figure(go.Bar(
                x=priced["unrealized"], y=priced["chart_label"], orientation="h",
                marker_color=[GAIN if v >= 0 else LOSS for v in priced["unrealized"]],
                customdata=priced[["market_value", "quantity"]],
                hovertemplate="%{y}<br>unrealized $%{x:,.2f}"
                              "<br>market value $%{customdata[0]:,.2f}"
                              "<br>qty %{customdata[1]:,.4f}<extra></extra>",
            ))
            fig.add_vline(x=0, line_width=1, line_color="black")
            fig.update_xaxes(tickformat=MONEY)
            figures.append(("08_holdings_pnl", layout(
                fig, f"Unrealized Gain/(Loss) by Position — {latest_month}",
                height=max(420, 26 * len(priced) + 140), rotate=False)))

        ordered = current.sort_values("market_value")
        fig = go.Figure(go.Bar(
            x=ordered["market_value"], y=ordered["chart_label"], orientation="h",
            marker_color=["#1f77b4" if v >= 0 else "#e67e22" for v in ordered["market_value"]],
            customdata=ordered[["quantity"]],
            hovertemplate="%{y}<br>market value $%{x:,.2f}"
                          "<br>qty %{customdata[0]:,.4f}<extra></extra>",
        ))
        fig.add_vline(x=0, line_width=1, line_color="black")
        fig.update_xaxes(tickformat=MONEY)
        figures.append(("09_position_exposure", layout(
            fig, f"Market Value by Position (negative = short) — {latest_month}",
            height=max(420, 26 * len(ordered) + 140), rotate=False)))

    return figures


def interim_breakdown_chart(interim: dict) -> go.Figure:
    """The confirm-feed window's P&L split into its two components.

    Deliberately kept separate from gain_breakdown_chart: this window has no
    dividends, interest, fees or corporate-action data, so its components are not
    the same kind of figure as a statement month's gain components and must not
    share a chart or a total with them.
    """
    value_change = interim.get("value_change") or 0.0
    trade_cash = interim.get("trade_cash") or 0.0
    pnl = interim.get("pnl")
    fig = go.Figure(go.Bar(
        x=["Position value change", "Trade cash"], y=[value_change, trade_cash],
        marker_color=[GAIN if value_change >= 0 else LOSS, GAIN if trade_cash >= 0 else LOSS],
        hovertemplate="%{x}<br>$%{y:,.2f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=["Profit and loss"], y=[0.0 if pnl is None else float(pnl)],
        marker_color=PRIMARY, hovertemplate="Profit and loss<br>$%{y:,.2f}<extra></extra>",
    ))
    fig.add_hline(y=0, line_width=1, line_color="black")
    fig.update_layout(showlegend=False)
    return layout(
        fig, f"Since {interim.get('anchor_date')} (unaudited, confirm feed)", height=340,
        y_title="$", tick_format=MONEY, rotate=False,
    )


def gain_breakdown_chart(row: pd.Series) -> go.Figure:
    """One statemented month's gain, split into its printed components.

    Mirrors the reconciliation build_frame already performs (gain versus market
    appreciation + dividends/interest + expenses), so the bars are components the
    statement itself prints, not a re-derivation. Never called with the unaudited
    interim window - that window has no dividends, interest or expense data, so its
    P&L is not comparable to a statement month's gain.
    """
    components = [
        ("Market appr.", row.get("market_appreciation")),
        ("Dividends & interest", row.get("dividends_interest")),
        ("Realized ST", row.get("st_net")),
        ("Realized LT", row.get("lt_net")),
        ("Expenses", row.get("expenses")),
    ]
    labels = [label for label, _ in components]
    values = [0.0 if value is None or pd.isna(value) else float(value) for _, value in components]
    fig = go.Figure(go.Bar(
        x=labels, y=values,
        marker_color=[GAIN if value >= 0 else LOSS for value in values],
        hovertemplate="%{x}<br>$%{y:,.2f}<extra></extra>",
    ))
    total = row.get("gain")
    fig.add_trace(go.Bar(
        x=["Total gain"], y=[0.0 if total is None or pd.isna(total) else float(total)],
        marker_color=PRIMARY, hovertemplate="Total gain<br>$%{y:,.2f}<extra></extra>",
    ))
    fig.add_hline(y=0, line_width=1, line_color="black")
    fig.update_layout(showlegend=False)
    return layout(
        fig, f"Gain breakdown — {row.get('month')}", height=380,
        y_title="$", tick_format=MONEY, rotate=False,
    )


def write_charts(frame: pd.DataFrame, holdings: pd.DataFrame, metrics: dict,
                 directory: Path) -> list:
    """Write every chart as standalone interactive HTML and return the filenames.

    kaleido is not installed, so there is no static image export. `plotly.js` is
    written once into the directory and shared by every page rather than loaded from
    a CDN, so the charts open offline and no browser request leaves the machine.
    """
    directory.mkdir(parents=True, exist_ok=True)
    names = []
    for slug, fig in build_charts(frame, holdings, metrics):
        name = f"{slug}.html"
        fig.write_html(
            directory / name,
            include_plotlyjs="directory",
            full_html=True,
            config={"displaylogo": False, "responsive": True},
        )
        names.append(name)
    if names:
        links = "\n".join(
            f'    <li><a href="{name}">{name[:-5].replace("_", " ")}</a></li>' for name in names
        )
        (directory / "index.html").write_text(
            "<!doctype html>\n<html><head><meta charset='utf-8'>"
            "<title>Schwab statement charts</title></head>\n"
            "<body style=\"font-family:system-ui;margin:2rem\">\n"
            "  <h1>Statement charts</h1>\n  <ul>\n" + links + "\n  </ul>\n</body></html>\n"
        )
        names.append("index.html")
    return names
