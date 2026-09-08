(function () {
  'use strict';

  const Charts = window.LightweightCharts;
  const familyColors = {
    zigzag: '#a78bfa',
    stochastic: '#3b82f6',
    multitimeframe: '#52d8e8',
    combined: '#ff4d67',
  };
  const variantLineStyles = [
    Charts.LineStyle.Solid,
    Charts.LineStyle.Dotted,
    Charts.LineStyle.Dashed,
    Charts.LineStyle.LargeDashed,
    Charts.LineStyle.SparseDotted,
  ];
  const money = value => new Intl.NumberFormat('pt-BR', {
    style: 'currency', currency: 'USD', maximumFractionDigits: 2,
  }).format(value || 0);
  const price = value => value == null ? '—' : Number(value).toFixed(3);

  function familyFor(profile) {
    const value = String(profile || '').toLowerCase();
    if (value.includes('portfolio')) return 'combined';
    if (value.includes('zigzag')) return 'zigzag';
    if (value.includes('stochastic')) return 'stochastic';
    if (value.includes('multitimeframe')) return 'multitimeframe';
    return 'combined';
  }

  function create(host, legend) {
    const chart = Charts.createChart(host, {
      autoSize: true,
      layout: {
        background: { type: Charts.ColorType.Solid, color: '#0b0f16' }, textColor: '#8491a5',
        fontFamily: "Inter, Arial, sans-serif", attributionLogo: false,
        panes: { separatorColor: 'rgba(148,163,184,.16)', separatorHoverColor: '#8b7cff', enableResize: true },
      },
      grid: {
        vertLines: { color: 'rgba(148,163,184,.055)' },
        horzLines: { color: 'rgba(148,163,184,.055)' },
      },
      rightPriceScale: { borderColor: 'rgba(148,163,184,.14)', scaleMargins: { top: 0.1, bottom: 0.1 } },
      timeScale: {
        borderColor: 'rgba(148,163,184,.14)', timeVisible: true, secondsVisible: false,
        rightOffset: 6, barSpacing: 7, minBarSpacing: 0.35,
      },
      crosshair: {
        mode: Charts.CrosshairMode.MagnetOHLC,
        vertLine: { color: 'rgba(139,124,255,.65)', labelBackgroundColor: '#594bc8' },
        horzLine: { color: 'rgba(139,124,255,.65)', labelBackgroundColor: '#594bc8' },
      },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
    });
    let candleSeries = null;
    let equitySeries = [];
    let candleRows = new Map();

    function clear() {
      for (const pane of [...chart.panes()].reverse()) {
        for (const series of pane.getSeries()) chart.removeSeries(series);
      }
      candleSeries = null;
      equitySeries = [];
      candleRows = new Map();
    }

    function setData(payload, options) {
      clear();
      candleSeries = chart.addSeries(Charts.CandlestickSeries, {
        upColor: '#28e0a3', downColor: '#ff5470', borderVisible: false,
        wickUpColor: '#28e0a3', wickDownColor: '#ff5470',
        priceFormat: { type: 'price', precision: 3, minMove: 0.001 },
      }, 0);
      const candles = (payload.candles || []).map(row => ({
        time: row.t / 1000, open: row.o, high: row.h, low: row.l, close: row.c,
      }));
      candleSeries.setData(candles);
      candleRows = new Map((payload.candles || []).map(row => [row.t / 1000, row]));

      const requestedIds = options && options.visibleBotIds ? new Set(options.visibleBotIds) : null;
      const familyVariantCounts = new Map();
      equitySeries = (payload.series || []).filter(item => item.points && item.points.length).map(item => {
        const family = familyFor(item.strategy_profile);
        const variantIndex = familyVariantCounts.get(family) || 0;
        familyVariantCounts.set(family, variantIndex + 1);
        const color = familyColors[family];
        const lineStyle = variantLineStyles[variantIndex % variantLineStyles.length];
        const visible = !requestedIds || requestedIds.has(item.bot_id);
        const api = chart.addSeries(Charts.LineSeries, {
          title: '', color, lineWidth: 2, lineStyle, visible,
          crosshairMarkerVisible: true, crosshairMarkerRadius: 4,
          lastValueVisible: false, priceLineVisible: false,
          priceFormat: { type: 'custom', formatter: money },
        }, 1);
        api.setData(item.points.map(point => ({
          time: Math.floor(Date.parse(`${point.day}T00:00:00Z`) / 1000),
          value: Number(point.cumulative_pnl),
        })));
        return { api, item, color, lineStyle, visible };
      });
      const panes = chart.panes();
      if (panes[0]) panes[0].setHeight(330);
      if (panes[1]) panes[1].setHeight(150);
      renderDefaultLegend();
      chart.timeScale().fitContent();
      return getSeriesState();
    }

    function getSeriesState() {
      return equitySeries.map(entry => ({
        botId: entry.item.bot_id,
        name: entry.item.name,
        strategyProfile: entry.item.strategy_profile,
        summary: entry.item.summary,
        color: entry.color,
        lineStyle: entry.lineStyle,
        visible: entry.visible,
      }));
    }

    function setVisibleBotIds(botIds) {
      const next = new Set(botIds);
      equitySeries.forEach(entry => {
        entry.visible = next.has(entry.item.bot_id);
        entry.api.applyOptions({ visible: entry.visible });
      });
      renderDefaultLegend();
      return getSeriesState();
    }

    function renderDefaultLegend() {
      legend.innerHTML = equitySeries.filter(entry => entry.visible).map(entry =>
        `<span class="legend-item"><i style="background:${entry.color}"></i>${escapeHtml(entry.item.name.replace(' · EUR/USD', ''))} · ${money(entry.item.summary.net_pnl)}</span>`
      ).join('');
    }

    chart.subscribeCrosshairMove(parameter => {
      if (!parameter.time || !parameter.point || parameter.point.x < 0 || parameter.point.y < 0) {
        renderDefaultLegend();
        return;
      }
      const candle = candleRows.get(parameter.time);
      const candleText = candle
        ? `<span class="ohlc">O <strong>${price(candle.o)}</strong> H <strong>${price(candle.h)}</strong> L <strong>${price(candle.l)}</strong> C <strong>${price(candle.c)}</strong></span>`
        : '';
      const values = equitySeries.filter(entry => entry.visible).map(entry => {
        const point = parameter.seriesData.get(entry.api);
        return point && point.value != null
          ? `<span class="legend-item"><i style="background:${entry.color}"></i>${escapeHtml(entry.item.name)} <strong>${money(point.value)}</strong></span>`
          : '';
      }).join('');
      legend.innerHTML = `<time>${new Date(Number(parameter.time) * 1000).toLocaleString('pt-BR', { timeZone: 'UTC' })} UTC</time>${candleText}${values}`;
    });

    return {
      setData,
      setVisibleBotIds,
      getSeriesState,
      fitContent: () => chart.timeScale().fitContent(),
      destroy: () => chart.remove(),
    };
  }

  window.LotusEquityChart = { create };
}());
