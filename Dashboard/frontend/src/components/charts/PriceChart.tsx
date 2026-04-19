"use client";

import { useEffect, useRef } from "react";
import { createChart, createSeriesMarkers, type IChartApi, type ISeriesApi, LineSeries, type LineData, type Time } from "lightweight-charts";
import type { PriceTick, TradeEvent } from "@/lib/types";

interface Props {
  ticks: PriceTick[];
  events: TradeEvent[];
  entryPrice?: number;
  exitPrice?: number;
  floorPrice?: number | null;
  height?: number;
  live?: boolean;
}

function toTime(ts: string): Time {
  return Math.floor(new Date(ts).getTime() / 1000) as Time;
}

export default function PriceChart({
  ticks,
  events,
  entryPrice,
  exitPrice,
  floorPrice,
  height = 300,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height,
      layout: {
        background: { color: "#12121a" },
        textColor: "#8892b0",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#1e1e2e" },
        horzLines: { color: "#1e1e2e" },
      },
      crosshair: {
        vertLine: { color: "#3a3a5a", labelBackgroundColor: "#1e1e2e" },
        horzLine: { color: "#3a3a5a", labelBackgroundColor: "#1e1e2e" },
      },
      timeScale: {
        borderColor: "#1e1e2e",
        timeVisible: true,
        secondsVisible: false,
      },
      rightPriceScale: {
        borderColor: "#1e1e2e",
      },
    });

    const series = chart.addSeries(LineSeries, {
      color: "#00d4ff",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
    });

    // Build data from ticks. lightweight-charts rejects duplicate times, and
    // toTime floors to seconds, so two ticks in the same second collapse —
    // keep the latest price for each second (ticks arrive sorted ascending).
    const data: LineData[] = [];
    for (const t of ticks) {
      const time = toTime(t.timestamp);
      const last = data[data.length - 1];
      if (last && last.time === time) {
        last.value = t.price;
      } else {
        data.push({ time, value: t.price });
      }
    }

    if (data.length > 0) {
      series.setData(data);
    }

    // Entry price line
    if (entryPrice !== undefined) {
      series.createPriceLine({
        price: entryPrice,
        color: "#00e5a0",
        lineWidth: 1,
        lineStyle: 2, // dashed
        axisLabelVisible: true,
        title: "Entry",
      });
    }

    // Exit price line
    if (exitPrice !== undefined) {
      series.createPriceLine({
        price: exitPrice,
        color: "#ff4757",
        lineWidth: 1,
        lineStyle: 2, // dashed
        axisLabelVisible: true,
        title: "Exit",
      });
    }

    // Floor price line
    if (floorPrice) {
      series.createPriceLine({
        price: floorPrice,
        color: "#ff6b6b",
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: "Floor",
      });
    }

    // Event markers
    const markers = events
      .filter((e) => e.event_type === "entry" || e.event_type === "exit")
      .map((e) => ({
        time: toTime(e.timestamp),
        position: e.event_type === "entry" ? "belowBar" as const : "aboveBar" as const,
        color: e.event_type === "entry" ? "#00e5a0" : "#ff4757",
        shape: e.event_type === "entry" ? "arrowUp" as const : "arrowDown" as const,
        text: e.event_type === "entry"
          ? `Entry $${e.price.toFixed(2)}`
          : `Exit $${e.price.toFixed(2)} (${e.reason})`,
      }));

    if (markers.length > 0) {
      createSeriesMarkers(series, markers);
    }

    chart.timeScale().fitContent();
    chartRef.current = chart;
    seriesRef.current = series;

    const handleResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, [ticks, events, entryPrice, exitPrice, floorPrice, height]);

  const resetZoom = () => {
    chartRef.current?.timeScale().fitContent();
  };

  return (
    <div className="relative">
      <div ref={containerRef} />
      <button
        onClick={resetZoom}
        className="absolute top-2 right-2 px-2 py-1 text-[10px] rounded bg-[#1e1e2e] text-[#8892b0] hover:text-[#ccd6f6] border border-[#2a2a3a] hover:border-[#3a3a5a] transition-colors"
      >
        Reset Zoom
      </button>
    </div>
  );
}
