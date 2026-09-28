import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  AreaSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";

const C = {
  bg: "#10151c",
  grid: "rgba(255,255,255,0.04)",
  text: "#8b95a5",
  border: "#1f2733",
  up: "#1fc27e",
  down: "#f0465a",
  ema7: "#f2b33d",
  ema25: "#4c8dff",
  ema99: "#b07bff",
};

function baseChart(el: HTMLElement, height: number): IChartApi {
  return createChart(el, {
    height,
    autoSize: true,
    layout: { background: { type: ColorType.Solid, color: C.bg }, textColor: C.text, fontSize: 11,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", attributionLogo: false },
    grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
    rightPriceScale: { borderColor: C.border },
    timeScale: { borderColor: C.border, timeVisible: true, secondsVisible: false },
    crosshair: { mode: CrosshairMode.Normal },
  });
}

export function ema(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null);
  if (values.length < period) return out;
  let prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
  out[period - 1] = prev;
  const a = 2 / (period + 1);
  for (let i = period; i < values.length; i++) {
    prev = a * values[i] + (1 - a) * prev;
    out[i] = prev;
  }
  return out;
}

export type Candle = [number, number, number, number, number, number?]; // time(s), o,h,l,c,v
export interface PriceLineSpec { price: number; color: string; title: string; dashed?: boolean }
export interface MarkerSpec { time: number; position: "aboveBar" | "belowBar"; color: string; shape: "arrowUp" | "arrowDown" | "circle"; text: string }

export function PriceChart({ candles, live, lines = [], markers = [], height = 360, showEma = true }: {
  candles: Candle[]; live?: Candle | null; lines?: PriceLineSpec[]; markers?: MarkerSpec[]; height?: number; showEma?: boolean;
}) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<{ c: ISeriesApi<"Candlestick">; e: ISeriesApi<"Line">[]; v: ISeriesApi<"Histogram"> } | null>(null);
  const priceLines = useRef<IPriceLine[]>([]);
  const markerApi = useRef<ReturnType<typeof createSeriesMarkers<Time>> | null>(null);
  const lastTime = useRef<number>(0);

  useEffect(() => {
    if (!el.current) return;
    const ch = baseChart(el.current, height);
    const c = ch.addSeries(CandlestickSeries, {
      upColor: C.up, downColor: C.down, borderUpColor: C.up, borderDownColor: C.down, wickUpColor: C.up, wickDownColor: C.down,
      priceFormat: { type: "price", precision: 1, minMove: 0.1 },
    });
    const v = ch.addSeries(HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    ch.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const e = [C.ema7, C.ema25, C.ema99].map((color) =>
      ch.addSeries(LineSeries, { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }));
    chart.current = ch;
    series.current = { c, e, v };
    markerApi.current = createSeriesMarkers(c, []);
    return () => {
      ch.remove();
      chart.current = null;
      series.current = null;
    };
  }, [height]);

  useEffect(() => {
    const s = series.current;
    if (!s) return;
    const t = (x: number) => x as UTCTimestamp;
    s.c.setData(candles.map(([time, o, h, l, cl]) => ({ time: t(time), open: o, high: h, low: l, close: cl })));
    s.v.setData(candles.map(([time, o, , , cl, vol]) => ({ time: t(time), value: vol ?? 0,
      color: cl >= o ? "rgba(31,194,126,0.35)" : "rgba(240,70,90,0.35)" })));
    const closes = candles.map((k) => k[4]);
    [7, 25, 99].forEach((p, i) => {
      const vals = ema(closes, p);
      s.e[i].setData(showEma ? candles.map((k, j) => (vals[j] == null ? { time: t(k[0]) } : { time: t(k[0]), value: vals[j] as number })) : []);
    });
    lastTime.current = candles.length ? candles[candles.length - 1][0] : 0;
  }, [candles, showEma]);

  useEffect(() => {
    const s = series.current;
    if (!s || !live || live[0] < lastTime.current) return;
    const [time, o, h, l, cl] = live;
    s.c.update({ time: time as UTCTimestamp, open: o, high: h, low: l, close: cl });
  }, [live]);

  useEffect(() => {
    const s = series.current;
    if (!s) return;
    priceLines.current.forEach((pl) => s.c.removePriceLine(pl));
    priceLines.current = lines.filter((l) => Number.isFinite(l.price)).map((l) =>
      s.c.createPriceLine({ price: l.price, color: l.color, lineWidth: 1, lineStyle: l.dashed ? LineStyle.Dashed : LineStyle.Solid,
        axisLabelVisible: true, title: l.title }));
  }, [lines]);

  useEffect(() => {
    markerApi.current?.setMarkers(markers.slice().sort((a, b) => a.time - b.time)
      .map((m) => ({ ...m, time: m.time as UTCTimestamp }) as SeriesMarker<Time>));
  }, [markers]);

  return <div ref={el} className="chart" style={{ height }} />;
}

export function LineChart({ data, height = 220, color = "#4c8dff", area = true, baseline }: {
  data: [number, number][]; height?: number; color?: string; area?: boolean; baseline?: number;
}) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!el.current) return;
    const ch = baseChart(el.current, height);
    ch.timeScale().applyOptions({ timeVisible: true });
    const s = area
      ? ch.addSeries(AreaSeries, { lineColor: color, topColor: color + "55", bottomColor: color + "05", lineWidth: 2, priceLineVisible: false })
      : ch.addSeries(LineSeries, { color, lineWidth: 2, priceLineVisible: false });
    const dedup = new Map<number, number>();
    data.forEach(([t, v]) => dedup.set(t, v));
    s.setData([...dedup.entries()].sort((a, b) => a[0] - b[0]).map(([t, v]) => ({ time: t as UTCTimestamp, value: v })));
    if (baseline !== undefined) s.createPriceLine({ price: baseline, color: "#5b6575", lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
    ch.timeScale().fitContent();
    return () => ch.remove();
  }, [data, height, color, area, baseline]);
  return <div ref={el} className="chart" style={{ height }} />;
}

export function BarsChart({ data, height = 200 }: { data: [number, number][]; height?: number }) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!el.current) return;
    const ch = baseChart(el.current, height);
    ch.timeScale().applyOptions({ timeVisible: false });
    const s = ch.addSeries(HistogramSeries, { priceLineVisible: false });
    s.setData(data.map(([t, v]) => ({ time: t as UTCTimestamp, value: v, color: v >= 0 ? C.up : C.down })));
    ch.timeScale().fitContent();
    return () => ch.remove();
  }, [data, height]);
  return <div ref={el} className="chart" style={{ height }} />;
}

/** Simple horizontal bar list (SVG-free, CSS) for categorical data. */
export function HBars({ items }: { items: { label: string; value: number; tone?: "green" | "red" | "blue" }[] }) {
  const max = Math.max(1, ...items.map((i) => Math.abs(i.value)));
  return (
    <div className="hbars">
      {items.map((i) => (
        <div className="hbar" key={i.label}>
          <span className="hb-l">{i.label}</span>
          <span className="hb-t"><span className={`hb-f f-${i.tone || "blue"}`} style={{ width: `${(Math.abs(i.value) / max) * 100}%` }} /></span>
          <span className="hb-v mono">{i.value}</span>
        </div>
      ))}
    </div>
  );
}
