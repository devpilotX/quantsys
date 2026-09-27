"""Draw docs/images/pipeline.svg: the seven stages of one decision bar, in the
terminal screenshots' palette."""
from pathlib import Path

BG, PANEL, BORDER, TEXT, DIM, GREEN, AMBER = "#000000", "#0a0a0a", "#2a2a2a", "#ffffff", "#7b7b7b", "#00cc66", "#ddaa00"
FONT = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

stages = [
    ("RISK", "kill switches", "drawdown throttle"),
    ("TIER", "capital ladder", "limits and costs"),
    ("REGIME", "Gaussian HMM", "three states"),
    ("SIGNALS", "six sleeves", "entry and stop"),
    ("KELLY", "edge-weighted", "regime-tilted"),
    ("SIZING", "vol target, caps", "whole lots, cost gate"),
    ("ORDERS", "diff the book", "anti-churn bands"),
]

W, H = 1070, 300
box_w, box_h, gap = 132, 96, 16
x0 = (W - (len(stages) * box_w + (len(stages) - 1) * gap)) / 2
y0 = 104

out = [
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
    f'aria-label="The decision pipeline: risk, tier, regime, signals, Kelly, sizing, orders">',
    f'<rect width="{W}" height="{H}" rx="8" fill="{BG}"/>',
    f'<rect width="{W}" height="30" rx="8" fill="{PANEL}"/><rect y="22" width="{W}" height="8" fill="{PANEL}"/>',
    *[f'<circle cx="{20 + i * 18}" cy="15" r="5.5" fill="{c}"/>' for i, c in enumerate(["#ff5f57", "#febc2e", "#28c840"])],
    f'<text x="{W / 2}" y="19.5" fill="{DIM}" font-family="{FONT}" font-size="12" text-anchor="middle">'
    f'DecisionEngine.decide(): one pass per 15-minute bar</text>',
    f'<g font-family="{FONT}">',
    f'<text x="{W / 2}" y="72" fill="{DIM}" font-size="13" text-anchor="middle">'
    f'MarketState: bars, equity, positions</text>',
]
for i, (name, a, b) in enumerate(stages):
    x = x0 + i * (box_w + gap)
    colour = AMBER if name in ("RISK", "SIZING") else GREEN
    out += [
        f'<rect x="{x:.1f}" y="{y0}" width="{box_w}" height="{box_h}" rx="6" fill="{PANEL}" stroke="{BORDER}"/>',
        f'<rect x="{x:.1f}" y="{y0}" width="{box_w}" height="3" rx="1.5" fill="{colour}"/>',
        f'<text x="{x + 12:.1f}" y="{y0 + 22}" fill="{DIM}" font-size="11">{i + 1}</text>',
        f'<text x="{x + box_w / 2:.1f}" y="{y0 + 44}" fill="{TEXT}" font-size="15" font-weight="700" text-anchor="middle">{name}</text>',
        f'<text x="{x + box_w / 2:.1f}" y="{y0 + 66}" fill="{DIM}" font-size="11" text-anchor="middle">{a}</text>',
        f'<text x="{x + box_w / 2:.1f}" y="{y0 + 82}" fill="{DIM}" font-size="11" text-anchor="middle">{b}</text>',
    ]
    if i < len(stages) - 1:
        ax = x + box_w + 3
        out.append(f'<path d="M{ax:.1f} {y0 + box_h / 2} h{gap - 6} m-4 -4 l4 4 l-4 4" stroke="{DIM}" fill="none" stroke-width="1.5"/>')
out += [
    f'<text x="{W / 2}" y="{y0 + box_h + 44}" fill="{DIM}" font-size="13" text-anchor="middle">'
    f'Decision: targets, orders and an audit trail of every veto and scaling</text>',
    "</g></svg>",
]
Path("docs/images/pipeline.svg").write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
print("ok")
