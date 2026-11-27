// Charts for figures extracted from documents: horizontal bars (grouped or stacked), a line over time,
// or a pie of shares. Every value is a verified figure from a cited source; each mark has a hover/focus
// tooltip naming the source page, and a table view shows the same data.
import { useLayoutEffect, useRef, useState } from "react";
import { citationPlace } from "../format";
import type { Chart, Citation } from "../types";

const BAR = 16; // bar thickness
const GAP = 2; // surface gap between touching marks
const ROW_GAP = 16;
const RADIUS = 4;

interface Hover {
  category: string;
  series: string;
  value: number;
  source: string | null;
  share?: number; // pie only: fraction of the whole
  x: number;
  y: number;
}

type HoverInfo = Omit<Hover, "x" | "y">;
type ShowFn = (event: React.MouseEvent | React.FocusEvent, h: HoverInfo) => void;

function niceMax(max: number): { top: number; step: number } {
  if (max <= 0) return { top: 1, step: 1 };
  const rough = max / 4;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= rough) ?? 10 * magnitude;
  return { top: Math.ceil(max / step) * step, step };
}

const format = (v: number) => v.toLocaleString(undefined, { maximumFractionDigits: 2 });
const compact = (v: number) => v.toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 });
const color = (i: number) => `var(--series-${(i % 8) + 1})`;

/** Bar with a rounded data end (right) and a square baseline end (left). */
function barPath(x0: number, x1: number, y: number, h: number, rounded: boolean): string {
  const r = rounded ? Math.min(RADIUS, (x1 - x0) / 2, h / 2) : 0;
  return `M${x0},${y} H${x1 - r} Q${x1},${y} ${x1},${y + r} V${y + h - r} Q${x1},${y + h} ${x1 - r},${y + h} H${x0} Z`;
}

export function ChartView({ chart, sources }: { chart: Chart; sources: Map<string, Citation> }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(560);
  const [hover, setHover] = useState<Hover | null>(null);
  const [showTable, setShowTable] = useState(false);
  const kind = chart.kind ?? "bar";

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(280, Math.floor(entry.contentRect.width))));
    observer.observe(el);
    return () => observer.disconnect();
  }, [showTable]);

  const show: ShowFn = (event, h) => {
    const rect = box.current!.getBoundingClientRect();
    const target = (event.currentTarget as SVGElement).getBoundingClientRect();
    setHover({ ...h, x: target.right - rect.left, y: target.top - rect.top + target.height / 2 });
  };
  const hide = () => setHover(null);

  const sourceLabel = (tag: string | null) => {
    const c = tag ? sources.get(tag) : undefined;
    return c ? `${c.filename}, ${citationPlace(c)}` : null;
  };
  const multiSeries = chart.series.length > 1;

  return (
    <figure className="viz-root chart">
      <figcaption className="chart-head">
        <div>
          <strong>{chart.title}</strong>
          {chart.unit && <span className="muted"> · {chart.unit}</span>}
        </div>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowTable(!showTable)}>
          {showTable ? "Show chart" : "Show table"}
        </button>
      </figcaption>

      {multiSeries && kind !== "pie" && (
        <ul className="chart-legend" aria-label="Legend">
          {chart.series.map((s, i) => (
            <li key={s.name}>
              <span className="chart-swatch" style={{ background: color(i) }} />
              {s.name}
              {kind === "combo" && chart.line_series?.includes(s.name) && <span className="muted">(line, right axis)</span>}
            </li>
          ))}
        </ul>
      )}

      {showTable ? (
        <table className="chart-table">
          <thead>
            <tr>
              <th scope="col">{chart.unit ? `Item (${chart.unit})` : "Item"}</th>
              {chart.series.map((s) => (
                <th key={s.name} scope="col">{s.name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {chart.categories.map((category, ci) => (
              <tr key={category}>
                <th scope="row">{category}</th>
                {chart.series.map((s) => (
                  <td key={s.name} title={sourceLabel(s.sources[ci]) ?? undefined}>
                    {s.values[ci] === null ? "—" : format(s.values[ci]!)}
                    {s.sources[ci] && <span className="cite-chip cite-chip-static">{s.sources[ci]!.slice(1)}</span>}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="chart-plot" ref={box} onMouseLeave={hide}>
          {kind === "combo" ? (
            <ComboPlot chart={chart} width={width} show={show} hide={hide} />
          ) : kind === "line" ? (
            <LinePlot chart={chart} width={width} show={show} hide={hide} />
          ) : kind === "pie" ? (
            <PiePlot chart={chart} show={show} hide={hide} />
          ) : (
            <BarPlot chart={chart} width={width} show={show} hide={hide} />
          )}
          {hover && (
            <div className="chart-tooltip" style={{ left: Math.min(hover.x + 8, width - 200), top: hover.y }}>
              <strong>{hover.category}</strong>
              <span>
                {multiSeries ? `${hover.series}: ` : ""}
                <b>{format(hover.value)}</b> {chart.unit}
                {hover.share !== undefined && ` (${Math.round(hover.share * 1000) / 10}%)`}
              </span>
              {sourceLabel(hover.source) && <span className="muted">Source: {sourceLabel(hover.source)}</span>}
            </div>
          )}
        </div>
      )}
    </figure>
  );
}

interface PlotProps {
  chart: Chart;
  show: ShowFn;
  hide: () => void;
}

function BarPlot({ chart, width, show, hide }: PlotProps & { width: number }) {
  const stacked = chart.layout === "stacked";
  const totals = chart.categories.map((_, ci) =>
    stacked
      ? chart.series.reduce((sum, s) => sum + (s.values[ci] ?? 0), 0)
      : Math.max(0, ...chart.series.map((s) => s.values[ci] ?? 0)),
  );
  const { top, step } = niceMax(Math.max(...totals));
  const labelWidth = Math.min(170, Math.round(width * 0.34));
  const plotLeft = labelWidth + 10;
  const plotRight = width - 44; // room for the value label at the bar end
  const x = (v: number) => plotLeft + ((plotRight - plotLeft) * v) / top;
  const rowHeight = stacked ? BAR : chart.series.length * BAR + (chart.series.length - 1) * GAP;
  const top0 = 8;
  const height = top0 + chart.categories.length * (rowHeight + ROW_GAP) + 22;
  const ticks = Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);

  return (
    <svg width={width} height={height} role="img" aria-label={`${chart.title}. Use "Show table" for the values.`}>
      {ticks.map((t) => (
        <g key={t}>
          <line className="chart-grid" x1={x(t)} x2={x(t)} y1={top0 - 4} y2={height - 20} />
          <text className="chart-tick" x={x(t)} y={height - 6} textAnchor="middle">
            {format(t)}
          </text>
        </g>
      ))}
      {chart.categories.map((category, ci) => {
        const rowY = top0 + ci * (rowHeight + ROW_GAP);
        let stackStart = 0;
        let lastIndex = -1; // the outermost segment of a stack gets the rounded data end
        chart.series.forEach((s, si) => {
          if ((s.values[ci] ?? 0) > 0) lastIndex = si;
        });
        return (
          <g key={category}>
            <text className="chart-label" x={labelWidth} y={rowY + rowHeight / 2} textAnchor="end" dominantBaseline="middle">
              {category.length > 26 ? `${category.slice(0, 25)}…` : category}
              <title>{category}</title>
            </text>
            {chart.series.map((s, si) => {
              const value = s.values[ci];
              if (value === null || value <= 0) return null;
              const x0 = stacked ? x(stackStart) + (stackStart > 0 ? GAP : 0) : x(0);
              const x1 = stacked ? x(stackStart + value) : x(value);
              const y = stacked ? rowY : rowY + si * (BAR + GAP);
              stackStart += value;
              const label = { category, series: s.name, value, source: s.sources[ci] };
              return (
                <g key={s.name}>
                  <path
                    className="chart-bar"
                    d={barPath(x0, Math.max(x1, x0 + 1), y, BAR, !stacked || si === lastIndex)}
                    fill={color(si)}
                    tabIndex={0}
                    aria-label={`${category}, ${s.name}: ${format(value)}${chart.unit ? ` ${chart.unit}` : ""}`}
                    onMouseEnter={(e) => show(e, label)}
                    onFocus={(e) => show(e, label)}
                    onBlur={hide}
                  />
                  {!stacked && (
                    <text className="chart-value" x={x1 + 6} y={y + BAR / 2} dominantBaseline="middle">
                      {format(value)}
                    </text>
                  )}
                </g>
              );
            })}
            {stacked && (
              <text className="chart-value" x={x(stackStart) + 6} y={rowY + BAR / 2} dominantBaseline="middle">
                {format(stackStart)}
              </text>
            )}
          </g>
        );
      })}
      <line className="chart-axis" x1={x(0)} x2={x(0)} y1={top0 - 4} y2={height - 20} />
    </svg>
  );
}

/** Values over time: categories along the x axis in the order given, one line per series. */
function LinePlot({ chart, width, show, hide }: PlotProps & { width: number }) {
  const values = chart.series.flatMap((s) => s.values.filter((v): v is number => v !== null));
  const { top, step } = niceMax(Math.max(0, ...values));
  const height = 250;
  const left = 56;
  const right = width - 16;
  const plotTop = 10;
  const bottom = height - 34;
  const n = chart.categories.length;
  const x = (i: number) => (n === 1 ? (left + right) / 2 : left + ((right - left) * i) / (n - 1));
  const y = (v: number) => bottom - ((bottom - plotTop) * Math.max(0, v)) / top;
  const ticks = Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);
  const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor((right - left) / 70)))); // label spacing

  return (
    <svg width={width} height={height} role="img" aria-label={`${chart.title}. Use "Show table" for the values.`}>
      {ticks.map((t) => (
        <g key={t}>
          <line className="chart-grid" x1={left} x2={right} y1={y(t)} y2={y(t)} />
          <text className="chart-tick" x={left - 8} y={y(t)} textAnchor="end" dominantBaseline="middle">
            {compact(t)}
          </text>
        </g>
      ))}
      <line className="chart-axis" x1={left} x2={right} y1={bottom} y2={bottom} />
      {chart.categories.map((category, i) =>
        i % every === 0 || i === n - 1 ? (
          <text key={category} className="chart-tick" x={x(i)} y={bottom + 18} textAnchor="middle">
            {category.length > 12 ? `${category.slice(0, 11)}…` : category}
            <title>{category}</title>
          </text>
        ) : null,
      )}
      {chart.series.map((s, si) => {
        // Gaps (missing values) break the line instead of drawing through them.
        const path = s.values
          .map((v, i) => (v === null ? null : `${i === 0 || s.values[i - 1] === null ? "M" : "L"}${x(i)},${y(v)}`))
          .filter(Boolean)
          .join(" ");
        return (
          <g key={s.name}>
            <path className="chart-line" d={path} stroke={color(si)} />
            {s.values.map((v, i) => {
              if (v === null) return null;
              const label = { category: chart.categories[i], series: s.name, value: v, source: s.sources[i] };
              return (
                <circle
                  key={chart.categories[i]}
                  className="chart-dot"
                  cx={x(i)}
                  cy={y(v)}
                  r={4}
                  fill={color(si)}
                  tabIndex={0}
                  aria-label={`${chart.categories[i]}, ${s.name}: ${format(v)}${chart.unit ? ` ${chart.unit}` : ""}`}
                  onMouseEnter={(e) => show(e, label)}
                  onFocus={(e) => show(e, label)}
                  onBlur={hide}
                />
              );
            })}
          </g>
        );
      })}
    </svg>
  );
}

/** Columns for most series plus line series on their own (right) axis, e.g. Debit/Credit with the Balance. */
function ComboPlot({ chart, width, show, hide }: PlotProps & { width: number }) {
  const lineNames = new Set(chart.line_series ?? []);
  const bars = chart.series.map((s, i) => ({ s, i })).filter(({ s }) => !lineNames.has(s.name));
  const lines = chart.series.map((s, i) => ({ s, i })).filter(({ s }) => lineNames.has(s.name));
  const maxOf = (group: typeof bars) => Math.max(0, ...group.flatMap(({ s }) => s.values.filter((v): v is number => v !== null)));
  const left = niceMax(maxOf(bars));
  const right = niceMax(maxOf(lines));
  const height = 260;
  const x0 = 56;
  const x1 = width - 56;
  const plotTop = 10;
  const bottom = height - 34;
  const n = chart.categories.length;
  const band = (x1 - x0) / n;
  const colWidth = Math.max(3, Math.min(22, (band * 0.7) / Math.max(1, bars.length)));
  const center = (i: number) => x0 + band * (i + 0.5);
  const yL = (v: number) => bottom - ((bottom - plotTop) * Math.max(0, v)) / left.top;
  const yR = (v: number) => bottom - ((bottom - plotTop) * Math.max(0, v)) / right.top;
  const ticks = Array.from({ length: Math.round(left.top / left.step) + 1 }, (_, i) => i * left.step);
  const every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor((x1 - x0) / 70))));

  return (
    <svg width={width} height={height} role="img" aria-label={`${chart.title}. Use "Show table" for the values.`}>
      {ticks.map((t) => (
        <g key={t}>
          <line className="chart-grid" x1={x0} x2={x1} y1={yL(t)} y2={yL(t)} />
          <text className="chart-tick" x={x0 - 8} y={yL(t)} textAnchor="end" dominantBaseline="middle">
            {compact(t)}
          </text>
          {lines.length > 0 && (
            <text className="chart-tick" x={x1 + 8} y={yL(t)} textAnchor="start" dominantBaseline="middle">
              {compact((t / left.top) * right.top)}
            </text>
          )}
        </g>
      ))}
      <line className="chart-axis" x1={x0} x2={x1} y1={bottom} y2={bottom} />
      {chart.categories.map((category, i) =>
        i % every === 0 || i === n - 1 ? (
          <text key={category} className="chart-tick" x={center(i)} y={bottom + 18} textAnchor="middle">
            {category.length > 12 ? `${category.slice(0, 11)}…` : category}
            <title>{category}</title>
          </text>
        ) : null,
      )}
      {chart.categories.map((category, ci) =>
        bars.map(({ s, i }, bi) => {
          const value = s.values[ci];
          if (value === null || value <= 0) return null;
          const xLeft = center(ci) - (bars.length * colWidth) / 2 + bi * colWidth;
          const label = { category, series: s.name, value, source: s.sources[ci] };
          return (
            <rect
              key={`${category}-${s.name}`}
              className="chart-bar"
              x={xLeft + 0.5}
              y={yL(value)}
              width={Math.max(1, colWidth - 1)}
              height={Math.max(1, bottom - yL(value))}
              rx={2}
              fill={color(i)}
              tabIndex={0}
              aria-label={`${category}, ${s.name}: ${format(value)}${chart.unit ? ` ${chart.unit}` : ""}`}
              onMouseEnter={(e) => show(e, label)}
              onFocus={(e) => show(e, label)}
              onBlur={hide}
            />
          );
        }),
      )}
      {lines.map(({ s, i }) => {
        const path = s.values
          .map((v, ci) => (v === null ? null : `${ci === 0 || s.values[ci - 1] === null ? "M" : "L"}${center(ci)},${yR(v)}`))
          .filter(Boolean)
          .join(" ");
        return (
          <g key={s.name}>
            <path className="chart-line" d={path} stroke={color(i)} />
            {s.values.map((v, ci) => {
              if (v === null) return null;
              const label = { category: chart.categories[ci], series: s.name, value: v, source: s.sources[ci] };
              return (
                <circle
                  key={chart.categories[ci]}
                  className="chart-dot"
                  cx={center(ci)}
                  cy={yR(v)}
                  r={4}
                  fill={color(i)}
                  tabIndex={0}
                  aria-label={`${chart.categories[ci]}, ${s.name}: ${format(v)}${chart.unit ? ` ${chart.unit}` : ""}`}
                  onMouseEnter={(e) => show(e, label)}
                  onFocus={(e) => show(e, label)}
                  onBlur={hide}
                />
              );
            })}
          </g>
        );
      })}
    </svg>
  );
}

/** Shares of one whole (one series): a donut with a legend of values and percentages. */
function PiePlot({ chart, show, hide }: PlotProps) {
  const series = chart.series[0];
  const slices = chart.categories
    .map((category, i) => ({ category, value: series.values[i] ?? 0, source: series.sources[i], index: i }))
    .filter((s) => s.value > 0);
  const total = slices.reduce((sum, s) => sum + s.value, 0) || 1;
  const size = 200;
  const c = size / 2;
  const outer = 92;
  const inner = 56;
  let angle = -Math.PI / 2;

  const point = (r: number, a: number) => `${c + r * Math.cos(a)},${c + r * Math.sin(a)}`;
  const arc = (a0: number, a1: number) => {
    const large = a1 - a0 > Math.PI ? 1 : 0;
    if (a1 - a0 >= 2 * Math.PI - 1e-6) a1 = a0 + 2 * Math.PI - 1e-4; // a single slice: (almost) a full ring
    return `M${point(outer, a0)} A${outer},${outer} 0 ${large} 1 ${point(outer, a1)} L${point(inner, a1)} A${inner},${inner} 0 ${large} 0 ${point(inner, a0)} Z`;
  };

  return (
    <div className="chart-pie">
      <svg width={size} height={size} role="img" aria-label={`${chart.title}. Use "Show table" for the values.`}>
        {slices.map((s) => {
          const a0 = angle;
          angle += (s.value / total) * 2 * Math.PI;
          const label = { category: s.category, series: series.name, value: s.value, source: s.source, share: s.value / total };
          return (
            <path
              key={s.category}
              className="chart-slice"
              d={arc(a0, angle)}
              fill={color(s.index)}
              tabIndex={0}
              aria-label={`${s.category}: ${format(s.value)} (${Math.round((s.value / total) * 1000) / 10}%)`}
              onMouseEnter={(e) => show(e, label)}
              onFocus={(e) => show(e, label)}
              onBlur={hide}
            />
          );
        })}
        <text className="chart-value" x={c} y={c - 6} textAnchor="middle">
          {compact(total)}
        </text>
        <text className="chart-tick" x={c} y={c + 12} textAnchor="middle">
          total
        </text>
      </svg>
      <ul className="chart-pie-legend" aria-label="Legend">
        {slices.map((s) => (
          <li key={s.category}>
            <span className="chart-swatch" style={{ background: color(s.index) }} />
            <span>{s.category}</span>
            <b>
              {format(s.value)} · {Math.round((s.value / total) * 1000) / 10}%
            </b>
          </li>
        ))}
      </ul>
    </div>
  );
}
