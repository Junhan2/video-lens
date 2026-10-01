// Benchmark charts drawn with Chart.js (loaded from cdnjs as window.Chart).
// Every number comes from data/benchmark.json. Models whose status is "pending" are skipped;
// a model added with data shows up as one more row in every chart without code changes.
// Colours: the two series use slots 1 and 2 of the dataviz reference palette; the comparison with other video
// skills adds slots 3 (/watch) and 4 (video-use) in the row order skillConditions() uses. Both sets were validated
// for light and dark with validate_palette.js. Text always uses text tokens, never series colours.

const SERIES = ['baseline', 'skill'];
const TASK_METRICS = ['time', 'cost', 'score'];
const activeCharts = new Map();
const taskView = { model: null, metric: 'time' };
const skillsView = { metric: 'score' };

export function measuredModels(data) {
  return Object.entries(data.models)
    .filter(([, model]) => model.status !== 'pending' && model.overall && model.per_task)
    .map(([id, model]) => ({ id, ...model }));
}

/** "23% lower" / "12% higher" / "no change", in the page language. */
export function describeChange(i18n, before, after) {
  const ratio = (after - before) / before;
  if (Math.abs(ratio) < 0.005) return i18n.t('change.same');
  return i18n.t(ratio < 0 ? 'change.lower' : 'change.higher', { pct: i18n.format.percent(Math.abs(ratio)) });
}

export function formatDate(code, isoDate) {
  const [year, month, day] = isoDate.split('-').map(Number);
  return new Intl.DateTimeFormat(code, { dateStyle: 'long', timeZone: 'UTC' }).format(new Date(Date.UTC(year, month - 1, day)));
}

export function taskLabel(i18n, task, length) {
  const key = `tasks.${task.id}.${length}`;
  return i18n.has(key) ? i18n.t(key) : task.label;
}

/** Chart labels round; tooltips and tables keep the JSON's precision. */
function costOrTime(fmt, isCost) {
  return {
    round: (value) => (isCost ? fmt.usd(value, 2) : fmt.seconds(value, 0)),
    exact: (value) => (isCost ? fmt.usd(value, 3) : fmt.seconds(value, 1)),
  };
}

/** Theme tokens; each series key reads its colour from --series-<key>. */
function readTheme(seriesKeys) {
  const style = getComputedStyle(document.documentElement);
  const token = (name) => style.getPropertyValue(name).trim();
  return {
    surface: token('--surface'), ink: token('--ink'), ink2: token('--ink-2'), muted: token('--muted'), grid: token('--grid'),
    axis: token('--axis'), font: token('--font-sans'),
    series: Object.fromEntries(seriesKeys.map((key) => [key, token(`--series-${key}`)])),
  };
}

/** A chart's datasets: model alone and with video-lens unless the spec passes its own. A colour may be one per row. */
function seriesOf({ theme, i18n }, spec) {
  return spec.series || SERIES.map((key) => ({ key, colour: theme.series[key], label: i18n.t(`series.${key}`) }));
}

function colourAt(entry, index) {
  return Array.isArray(entry.colour) ? entry.colour[index] : entry.colour;
}

function mixHex(colour, other, weight) {
  const channels = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const [a, b] = [channels(colour), channels(other)];
  return `#${a.map((value, i) => Math.round(value * (1 - weight) + b[i] * weight).toString(16).padStart(2, '0')).join('')}`;
}

function isReducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function svgIcon(shape, colour) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  svg.classList.add('legend-icon');
  const shapes = {
    rect: ['rect', { x: 2, y: 3, width: 12, height: 10, rx: 2 }],
    circle: ['circle', { cx: 8, cy: 8, r: 5 }],
    diamond: ['rect', { x: 4, y: 4, width: 8, height: 8, transform: 'rotate(45 8 8)' }],
    line: ['rect', { x: 1, y: 7, width: 14, height: 2, rx: 1 }],
  };
  const [tag, attributes] = shapes[shape];
  const mark = document.createElementNS(ns, tag);
  Object.entries(attributes).forEach(([name, value]) => mark.setAttribute(name, value));
  mark.setAttribute('fill', colour);
  svg.append(mark);
  return svg;
}

function buildLegend(entries) {
  const list = node('ul', 'legend');
  for (const entry of entries) {
    const item = node('li');
    item.append(svgIcon(entry.shape, entry.colour), node('span', null, entry.label));
    list.append(item);
  }
  return list;
}

function buildTable(columns, rows) {
  const wrap = node('div', 'table-wrap');
  const table = node('table');
  const head = node('thead');
  const headRow = node('tr');
  columns.forEach((column) => {
    const cell = node('th', column.isNumeric ? 'num' : null, column.label);
    cell.scope = 'col';
    headRow.append(cell);
  });
  head.append(headRow);
  const body = node('tbody');
  rows.forEach((row) => {
    const line = node('tr');
    row.forEach((value, i) => {
      const cell = node(i === 0 ? 'th' : 'td', columns[i].isNumeric ? 'num' : null, value);
      if (i === 0) cell.scope = 'row';
      line.append(cell);
    });
    body.append(line);
  });
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

/** Card chrome shared by every chart: title, subtitle, legend, canvas, captions, table twin, source. */
function buildCard(card, context, spec) {
  const { i18n, data } = context;
  card.textContent = '';
  const titleId = `${card.id}-title`;
  card.setAttribute('aria-labelledby', titleId);
  const head = node('div', 'chart-head');
  const title = node('h3', 'chart-title', spec.title);
  title.id = titleId;
  head.append(title, node('p', 'chart-sub', spec.subtitle));
  card.append(head);
  if (spec.controls) card.append(spec.controls);
  if (spec.legend.length) card.append(buildLegend(spec.legend));

  const box = node('div', 'chart-box');
  box.style.height = `${spec.height}px`;
  const canvas = node('canvas');
  canvas.tabIndex = 0;
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', `${spec.title}. ${i18n.t('chart.keyboard_hint')}`);
  const tip = node('div', 'chart-tip');
  tip.hidden = true;
  tip.setAttribute('aria-hidden', 'true');
  box.append(canvas, tip);
  const live = node('p', 'visually-hidden');
  live.setAttribute('aria-live', 'polite');
  card.append(box, live);

  if (spec.captions && spec.captions.length) {
    const captions = node('ul', 'chart-captions');
    spec.captions.forEach((text) => captions.append(node('li', null, text)));
    card.append(captions);
  }

  const footer = node('div', 'chart-foot');
  card.append(footer);
  if (context.isSolo) {
    footer.append(node('p', 'chart-source', i18n.t('chart.source', { date: formatDate(i18n.code, data.measured_on) })));
    return { canvas, tip, live };
  }
  const toggle = node('button', 'text-button table-toggle');
  toggle.type = 'button';
  const tableId = `${card.id}-table`;
  toggle.setAttribute('aria-controls', tableId);
  const table = buildTable(spec.table.columns, spec.table.rows);
  table.id = tableId;
  // Every chart has a toggle, so the accessible name carries the chart title.
  const showState = (isOpen) => {
    table.hidden = !isOpen;
    toggle.setAttribute('aria-expanded', String(isOpen));
    toggle.textContent = i18n.t(isOpen ? 'chart.hide_table' : 'chart.show_table');
    toggle.setAttribute('aria-label', i18n.t(isOpen ? 'chart.hide_table_aria' : 'chart.show_table_aria', { chart: spec.title }));
  };
  showState(false);
  toggle.addEventListener('click', () => showState(table.hidden));
  footer.append(toggle);
  card.append(table);
  return { canvas, tip, live };
}

function renderTooltip(tip, box, caret, content) {
  tip.textContent = '';
  tip.append(node('div', 'tip-title', content.title));
  for (const group of content.groups) {
    const single = group.items.length === 1 && !group.items[0].label;
    const row = node('div', 'tip-row');
    const key = node('span', 'tip-key');
    key.style.background = group.colour;
    row.append(key);
    if (single) row.append(node('strong', 'tip-value', group.items[0].value));
    row.append(node('span', 'tip-label', group.name));
    tip.append(row);
    if (single) continue;
    for (const item of group.items) {
      const line = node('div', 'tip-row tip-sub');
      line.append(node('strong', 'tip-value', item.value), node('span', 'tip-label', item.label));
      tip.append(line);
    }
  }
  tip.hidden = false;
  const left = caret.x + 14 + tip.offsetWidth > box.clientWidth ? caret.x - tip.offsetWidth - 14 : caret.x + 14;
  const top = Math.min(Math.max(caret.y - tip.offsetHeight / 2, 0), Math.max(box.clientHeight - tip.offsetHeight, 0));
  tip.style.transform = `translate(${Math.max(left, 0)}px, ${top}px)`;
}

function contentToText(content) {
  const groups = content.groups.map((group) => `${group.name}: ${group.items.map((item) => `${item.value}${item.label ? ` ${item.label}` : ''}`).join(', ')}`);
  return `${content.title}. ${groups.join('. ')}.`;
}

function externalTooltip(parts, describe) {
  return ({ chart, tooltip }) => {
    if (!tooltip || tooltip.opacity === 0 || !tooltip.dataPoints || !tooltip.dataPoints.length) {
      parts.tip.hidden = true;
      return;
    }
    renderTooltip(parts.tip, chart.canvas.parentElement, { x: tooltip.caretX, y: tooltip.caretY }, describe(tooltip.dataPoints[0].dataIndex));
  };
}

/** Arrow keys walk the rows; the tooltip shows and a live region reads the same values. */
function enableKeyboard(chart, parts, count, describe) {
  let index = -1;
  const show = (next) => {
    index = next;
    const active = chart.data.datasets.map((_, datasetIndex) => ({ datasetIndex, index }));
    const anchor = chart.getDatasetMeta(0).data[index];
    chart.setActiveElements(active);
    chart.tooltip.setActiveElements(active, { x: anchor.x, y: anchor.y });
    chart.update('none');
    parts.live.textContent = contentToText(describe(index));
  };
  const steps = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
  parts.canvas.addEventListener('keydown', (event) => {
    if (event.key === 'Home') show(0);
    else if (event.key === 'End') show(count - 1);
    else if (event.key in steps) show((Math.max(index, 0) + steps[event.key] + count) % count);
    else return;
    event.preventDefault();
  });
  parts.canvas.addEventListener('focus', () => show(Math.max(index, 0)));
  parts.canvas.addEventListener('blur', () => {
    chart.setActiveElements([]);
    chart.tooltip.setActiveElements([], { x: 0, y: 0 });
    chart.update('none');
    parts.tip.hidden = true;
  });
}

function baseOptions(theme, parts, describe, isSolo) {
  return {
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
    animation: isSolo || isReducedMotion() ? false : { duration: 450, easing: 'easeOutQuart' },
    interaction: { mode: 'index', axis: 'y', intersect: false },
    plugins: { legend: { display: false }, tooltip: { enabled: false, external: externalTooltip(parts, describe) } },
    scales: {
      y: {
        grid: { display: false },
        border: { color: theme.axis, width: 1 },
        ticks: {
          color: theme.ink2,
          padding: 8,
          font: { size: 12.5 },
          callback(value) {
            const label = this.getLabelForValue(value);
            return this.chart.width < 420 ? wrapLabel(label, 15) : label;
          },
        },
      },
    },
  };
}

/** Splits a long axis label into lines of about `limit` characters, at spaces. */
function wrapLabel(label, limit) {
  const lines = [];
  for (const word of String(label).split(' ')) {
    const last = lines[lines.length - 1];
    if (last && `${last} ${word}`.length <= limit) lines[lines.length - 1] = `${last} ${word}`;
    else lines.push(word);
  }
  return lines;
}

/** Horizontal grouped bars, one row per model or task, value at each bar's tip. */
function drawBars(card, context, spec) {
  const { theme } = context;
  const series = seriesOf(context, spec);
  const parts = buildCard(card, context, {
    ...spec,
    legend: spec.legend || series.map((entry) => ({ shape: 'rect', colour: entry.colour, label: entry.label })),
  });
  const valueLabels = {
    id: 'vlValueLabels',
    afterDatasetsDraw(chart) {
      const { ctx } = chart;
      ctx.save();
      ctx.font = `12px ${theme.font}`;
      ctx.fillStyle = theme.ink2;
      ctx.textBaseline = 'middle';
      chart.data.datasets.forEach((dataset, datasetIndex) => {
        chart.getDatasetMeta(datasetIndex).data.forEach((bar, index) => {
          const { x, y } = bar.getProps(['x', 'y'], true);
          ctx.fillText(spec.label(dataset.data[index]), x + 6, y);
        });
      });
      ctx.restore();
    },
  };
  const options = baseOptions(theme, parts, spec.describe, context.isSolo);
  options.layout = { padding: { right: 58, top: 2 } };
  options.scales.x = {
    beginAtZero: true,
    grid: { color: theme.grid, lineWidth: 1, drawTicks: false },
    border: { display: false },
    ticks: { color: theme.muted, padding: 6, maxTicksLimit: 6, callback: (value) => spec.label(value) },
  };
  const chart = new window.Chart(parts.canvas, {
    type: 'bar',
    data: {
      labels: spec.rows.map((row) => row.label),
      datasets: series.map((entry) => ({
        label: entry.label,
        data: spec.rows.map((row) => row[entry.key]),
        backgroundColor: entry.colour,
        hoverBackgroundColor: spec.rows.map((_, index) => mixHex(colourAt(entry, index), theme.surface, 0.28)),
        borderRadius: 4,
        borderSkipped: 'start',
        maxBarThickness: 20,
        categoryPercentage: 0.74,
        barPercentage: 0.84,
      })),
    },
    options,
    plugins: [valueLabels],
  });
  enableKeyboard(chart, parts, spec.rows.length, spec.describe);
  return chart;
}

/**
 * Dumbbell on a fixed 0 to 1 axis: a thin line from the worst run (diamond) to the mean or median (circle).
 * Markers keep one size; the hovered or focused row gets an ink ring instead.
 */
function drawDumbbell(card, context, spec) {
  const { theme, i18n } = context;
  const series = seriesOf(context, spec);
  const parts = buildCard(card, context, {
    ...spec,
    legend: [
      ...(spec.legend || series.map((entry) => ({ shape: 'line', colour: entry.colour, label: entry.label }))),
      { shape: 'circle', colour: theme.ink2, label: spec.highName },
      { shape: 'diamond', colour: theme.ink2, label: i18n.t('series.worst') },
    ],
  });
  const markers = {
    id: 'vlDumbbellMarkers',
    afterDatasetsDraw(chart) {
      const { ctx } = chart;
      const scale = chart.scales.x;
      const activeRows = new Set(chart.getActiveElements().map((element) => element.index));
      ctx.save();
      ctx.font = `12px ${theme.font}`;
      ctx.textBaseline = 'middle';
      series.forEach((entry, datasetIndex) => {
        chart.getDatasetMeta(datasetIndex).data.forEach((bar, index) => {
          const { y } = bar.getProps(['y'], true);
          const { high, low } = spec.rows[index][entry.key];
          const radius = 5;
          const isActive = activeRows.has(index);
          const xHigh = scale.getPixelForValue(high);
          const xLow = scale.getPixelForValue(low);
          const paint = (draw, size) => {
            if (isActive) {
              ctx.fillStyle = theme.ink;
              draw(size + 3.5);
            }
            ctx.fillStyle = theme.surface;
            draw(size + 2);
            ctx.fillStyle = colourAt(entry, index);
            draw(size);
          };
          const diamond = (size) => {
            ctx.beginPath();
            ctx.moveTo(xLow, y - size - 1);
            ctx.lineTo(xLow + size + 1, y);
            ctx.lineTo(xLow, y + size + 1);
            ctx.lineTo(xLow - size - 1, y);
            ctx.closePath();
            ctx.fill();
          };
          const circle = (size) => {
            ctx.beginPath();
            ctx.arc(xHigh, y, size, 0, Math.PI * 2);
            ctx.fill();
          };
          paint(diamond, radius);
          paint(circle, radius);
          ctx.fillStyle = theme.ink2;
          const isSplit = Math.abs(high - low) > 0.0005;
          if (spec.showLabel(high)) {
            ctx.textAlign = 'left';
            ctx.fillText(spec.label(high), xHigh + radius + 6, y);
          }
          if (isSplit && spec.showLabel(low)) {
            const text = spec.label(low);
            const width = ctx.measureText(text).width;
            if (xLow - radius - 6 - width >= chart.chartArea.left) {
              ctx.textAlign = 'right';
              ctx.fillText(text, xLow - radius - 6, y);
            } else {
              const x = xLow + radius + 6;
              ctx.fillStyle = theme.surface;
              ctx.fillRect(x - 3, y - 8, width + 6, 16);
              ctx.fillStyle = theme.ink2;
              ctx.textAlign = 'left';
              ctx.fillText(text, x, y);
            }
          }
        });
      });
      ctx.restore();
    },
  };
  const options = baseOptions(theme, parts, spec.describe, context.isSolo);
  options.layout = { padding: { right: 48, left: 4, top: 2 } };
  options.scales.x = {
    min: 0,
    max: 1,
    grid: { color: theme.grid, lineWidth: 1, drawTicks: false },
    border: { display: false },
    ticks: { color: theme.muted, padding: 6, stepSize: 0.2, callback: (value) => i18n.format.score(value, 1) },
  };
  const chart = new window.Chart(parts.canvas, {
    type: 'bar',
    data: {
      labels: spec.rows.map((row) => row.label),
      datasets: series.map((entry) => ({
        label: entry.label,
        data: spec.rows.map((row) => [row[entry.key].low, row[entry.key].high]),
        backgroundColor: entry.colour,
        hoverBackgroundColor: entry.colour,
        maxBarThickness: 2,
        categoryPercentage: 0.62,
        barPercentage: 1,
        borderSkipped: false,
      })),
    },
    options,
    plugins: [markers],
  });
  enableKeyboard(chart, parts, spec.rows.length, spec.describe);
  return chart;
}

function overallBars(metric, context) {
  const { i18n, data, models } = context;
  const isCost = metric === 'cost';
  const field = isCost ? 'cost_usd' : 'time_s';
  const fmt = i18n.format;
  const { round, exact } = costOrTime(fmt, isCost);
  const describe = (index) => ({
    title: models[index].label,
    groups: SERIES.map((series) => ({
      colour: context.theme.series[series],
      name: i18n.t(`series.${series}`),
      items: [{ value: exact(models[index].overall[series][field]) }],
    })),
  });
  return {
    title: i18n.t(`chart.${metric}_title`),
    subtitle: i18n.t(`chart.${metric}_sub`, { runs: fmt.integer(data.runs_per_condition) }),
    height: models.length * 60 + 40,
    rows: models.map((model) => ({ label: model.label, baseline: model.overall.baseline[field], skill: model.overall.skill[field] })),
    label: round,
    describe,
    captions: models.map((model) => i18n.t(`chart.${metric}_caption`, {
      model: model.label,
      change: describeChange(i18n, model.overall.baseline[field], model.overall.skill[field]),
    })),
    table: {
      columns: [
        { label: i18n.t('table.model') },
        { label: i18n.t('series.baseline'), isNumeric: true },
        { label: i18n.t('series.skill'), isNumeric: true },
        { label: i18n.t('table.change'), isNumeric: true },
      ],
      rows: models.map((model) => {
        const [before, after] = SERIES.map((series) => model.overall[series][field]);
        return [model.label, exact(before), exact(after), describeChange(i18n, before, after)];
      }),
    },
  };
}

/** "15/27": the runs of one condition that scored 1.0, out of all its runs. */
function perfectRuns(i18n, overall) {
  return i18n.t('format.of', { a: i18n.format.integer(overall.perfect_runs), b: i18n.format.integer(overall.runs) });
}

/** Tooltip lines for one condition's accuracy: mean score, worst run, runs scoring 1.0. */
function scoreItems(i18n, overall) {
  return [
    { value: i18n.format.score(overall.mean_score), label: i18n.t('series.mean') },
    { value: i18n.format.score(overall.worst_score), label: i18n.t('series.worst') },
    { value: perfectRuns(i18n, overall), label: i18n.t('table.perfect_runs') },
  ];
}

function accuracySpec(context) {
  const { i18n, data, models } = context;
  const fmt = i18n.format;
  return {
    title: i18n.t('chart.accuracy_title'),
    subtitle: i18n.t('chart.accuracy_sub', { runs: fmt.integer(data.runs_per_condition) }),
    highName: i18n.t('series.mean'),
    height: models.length * 66 + 40,
    rows: models.map((model) => ({
      label: model.label,
      baseline: { high: model.overall.baseline.mean_score, low: model.overall.baseline.worst_score },
      skill: { high: model.overall.skill.mean_score, low: model.overall.skill.worst_score },
    })),
    label: (value) => fmt.score(value, 3),
    showLabel: () => true,
    describe: (index) => ({
      title: models[index].label,
      groups: SERIES.map((series) => ({
        colour: context.theme.series[series],
        name: i18n.t(`series.${series}`),
        items: scoreItems(i18n, models[index].overall[series]),
      })),
    }),
    captions: models.map((model) => i18n.t('chart.accuracy_caption', {
      model: model.label,
      runs: fmt.integer(model.overall.skill.runs),
      skill_perfect: fmt.integer(model.overall.skill.perfect_runs),
      base_perfect: fmt.integer(model.overall.baseline.perfect_runs),
    })),
    table: {
      columns: [
        { label: i18n.t('table.model') }, { label: i18n.t('table.condition') },
        { label: i18n.t('table.mean_score'), isNumeric: true }, { label: i18n.t('table.worst_score'), isNumeric: true },
        { label: i18n.t('table.perfect_runs'), isNumeric: true },
      ],
      rows: models.flatMap((model) => SERIES.map((series) => [
        model.label, i18n.t(`series.${series}`), fmt.score(model.overall[series].mean_score),
        fmt.score(model.overall[series].worst_score), perfectRuns(i18n, model.overall[series]),
      ])),
    },
  };
}

function taskControls(context, onChange) {
  const { i18n, models } = context;
  const bar = node('div', 'chart-controls');
  const modelLabel = node('label', 'control');
  modelLabel.append(node('span', 'control-label', i18n.t('chart.model_label')));
  const select = node('select');
  select.setAttribute('aria-label', i18n.t('chart.model_label'));
  models.forEach((model) => {
    const option = node('option', null, model.label);
    option.value = model.id;
    option.selected = model.id === taskView.model;
    select.append(option);
  });
  select.addEventListener('change', () => onChange({ model: select.value }));
  modelLabel.append(select);
  bar.append(modelLabel, metricControl(i18n, taskView.metric, onChange));
  return bar;
}

/** Time, cost and score as one row of toggle buttons, `current` pressed. */
function metricControl(i18n, current, onChange) {
  const group = node('div', 'segmented');
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', i18n.t('chart.metric_label'));
  TASK_METRICS.forEach((metric) => {
    const button = node('button', 'segment', i18n.t(`chart.metric_${metric}`));
    button.type = 'button';
    button.setAttribute('aria-pressed', String(metric === current));
    button.addEventListener('click', () => onChange({ metric }));
    group.append(button);
  });
  const metricLabel = node('div', 'control');
  metricLabel.append(node('span', 'control-label', i18n.t('chart.metric_label')), group);
  return metricLabel;
}

function tasksSpec(context, onChange) {
  const { i18n, data, models } = context;
  const fmt = i18n.format;
  const model = models.find((candidate) => candidate.id === taskView.model) || models[0];
  const tasks = data.tasks.filter((task) => model.per_task[task.id]);
  const cell = (task, series) => model.per_task[task.id][series];
  const reps = fmt.integer(data.runs_per_task);
  const common = {
    title: i18n.t('chart.tasks_title'),
    subtitle: i18n.t(`chart.tasks_sub_${taskView.metric}`, { model: model.label, reps }),
    controls: context.isSolo ? null : taskControls(context, onChange),
    height: tasks.length * 46 + 40,
    table: {
      columns: [
        { label: i18n.t('table.task') }, { label: i18n.t('table.condition') },
        { label: i18n.t('table.median_score'), isNumeric: true }, { label: i18n.t('table.worst_score'), isNumeric: true },
        { label: i18n.t('table.median_cost'), isNumeric: true }, { label: i18n.t('table.median_time'), isNumeric: true },
      ],
      rows: tasks.flatMap((task) => SERIES.map((series) => [
        taskLabel(i18n, task, 'long'), i18n.t(`series.${series}`), fmt.score(cell(task, series).median_score),
        fmt.score(cell(task, series).worst_score), fmt.usd(cell(task, series).median_cost_usd, 3),
        fmt.seconds(cell(task, series).median_time_s, 1),
      ])),
    },
  };
  if (taskView.metric === 'score') {
    return {
      kind: 'dumbbell',
      ...common,
      highName: i18n.t('series.median'),
      rows: tasks.map((task) => ({
        label: taskLabel(i18n, task, 'short'),
        baseline: { high: cell(task, 'baseline').median_score, low: cell(task, 'baseline').worst_score },
        skill: { high: cell(task, 'skill').median_score, low: cell(task, 'skill').worst_score },
      })),
      label: (value) => fmt.score(value, 3),
      showLabel: (value) => value < 1,
      describe: (index) => ({
        title: taskLabel(i18n, tasks[index], 'long'),
        groups: SERIES.map((series) => ({
          colour: context.theme.series[series],
          name: i18n.t(`series.${series}`),
          items: [
            { value: fmt.score(cell(tasks[index], series).median_score), label: i18n.t('series.median') },
            { value: fmt.score(cell(tasks[index], series).worst_score), label: i18n.t('series.worst') },
          ],
        })),
      }),
    };
  }
  const isCost = taskView.metric === 'cost';
  const field = isCost ? 'median_cost_usd' : 'median_time_s';
  const { round, exact } = costOrTime(fmt, isCost);
  return {
    kind: 'bars',
    ...common,
    rows: tasks.map((task) => ({ label: taskLabel(i18n, task, 'short'), baseline: cell(task, 'baseline')[field], skill: cell(task, 'skill')[field] })),
    label: round,
    describe: (index) => ({
      title: taskLabel(i18n, tasks[index], 'long'),
      groups: SERIES.map((series) => ({
        colour: context.theme.series[series],
        name: i18n.t(`series.${series}`),
        items: [{ value: exact(cell(tasks[index], series)[field]) }],
      })),
    }),
  };
}

/**
 * The comparison with other video skills: the model they ran on, its measured competitor arms, and the condition keys
 * in row order (alone, video-lens, then each competitor), which is the order validate_palette.js passed (orange never
 * next to yellow). Competitor cells sit in the model's overall and per_task under the arm id. keys is empty until a
 * competitor has data.
 */
export function skillConditions(data) {
  const model = data.skills && measuredModels(data).find((candidate) => candidate.id === data.skills.model);
  const arms = model ? data.skills.arms.filter((arm) => arm.status !== 'pending' && model.overall[arm.id]) : [];
  return { model, arms, keys: arms.length ? [...SERIES, ...arms.map((arm) => arm.id)] : [] };
}

function skillsSpec(context, onChange) {
  const { i18n, data, theme } = context;
  const { model, keys } = skillConditions(data);
  if (!keys.length) return null;
  const fmt = i18n.format;
  const name = (key) => i18n.t(`series.${key}`);
  const overall = (index) => model.overall[keys[index]];
  const common = {
    title: i18n.t('chart.skills_title'),
    subtitle: i18n.t(`chart.skills_sub_${skillsView.metric}`, { model: model.label, runs: fmt.integer(data.runs_per_condition) }),
    controls: context.isSolo ? null : node('div', 'chart-controls'),
    legend: [],
    series: [{ key: 'value', colour: keys.map((key) => theme.series[key]), label: model.label }],
    height: keys.length * 46 + 40,
    table: {
      columns: [
        { label: i18n.t('table.condition') }, { label: i18n.t('table.mean_score'), isNumeric: true },
        { label: i18n.t('table.worst_score'), isNumeric: true }, { label: i18n.t('table.perfect_runs'), isNumeric: true },
        { label: i18n.t('table.cost'), isNumeric: true }, { label: i18n.t('table.time'), isNumeric: true },
        { label: i18n.t('table.own_analysis'), isNumeric: true },
      ],
      rows: keys.map((key, index) => [
        name(key), fmt.score(overall(index).mean_score), fmt.score(overall(index).worst_score), perfectRuns(i18n, overall(index)),
        fmt.usd(overall(index).cost_usd, 3), fmt.seconds(overall(index).time_s, 1),
        i18n.t('format.of', { a: fmt.integer(overall(index).own_analysis_runs), b: fmt.integer(overall(index).runs) }),
      ]),
    },
  };
  common.controls?.append(metricControl(i18n, skillsView.metric, onChange));
  const describe = (index, items) => ({
    title: name(keys[index]),
    groups: [{ colour: theme.series[keys[index]], name: model.label, items }],
  });
  if (skillsView.metric === 'score') {
    return {
      kind: 'dumbbell',
      ...common,
      highName: i18n.t('series.mean'),
      rows: keys.map((key, index) => ({ label: name(key), value: { high: overall(index).mean_score, low: overall(index).worst_score } })),
      label: (value) => fmt.score(value, 3),
      showLabel: () => true,
      describe: (index) => describe(index, scoreItems(i18n, overall(index))),
    };
  }
  const isCost = skillsView.metric === 'cost';
  const field = isCost ? 'cost_usd' : 'time_s';
  const { round, exact } = costOrTime(fmt, isCost);
  return {
    kind: 'bars',
    ...common,
    rows: keys.map((key, index) => ({ label: name(key), value: overall(index)[field] })),
    label: round,
    describe: (index) => describe(index, [{ value: exact(overall(index)[field]) }]),
  };
}

/** Charts with a measure toggle: the view they read and their spec builder, which returns null when there is no data. */
const TOGGLED_CHARTS = { tasks: { view: taskView, build: tasksSpec }, skills: { view: skillsView, build: skillsSpec } };

function drawChart(card, context) {
  const id = card.dataset.chart;
  activeCharts.get(id)?.destroy();
  let chart;
  if (id === 'cost' || id === 'time') chart = drawBars(card, context, overallBars(id, context));
  else if (id === 'accuracy') chart = drawDumbbell(card, context, accuracySpec(context));
  else if (TOGGLED_CHARTS[id]) {
    const { view, build } = TOGGLED_CHARTS[id];
    const redraw = (change) => {
      Object.assign(view, change);
      drawChart(card, context);
      card.querySelector(change.model ? '.chart-controls select' : '.segment[aria-pressed="true"]')?.focus();
    };
    const spec = build(context, redraw);
    if (spec) chart = spec.kind === 'dumbbell' ? drawDumbbell(card, context, spec) : drawBars(card, context, spec);
  }
  if (chart) activeCharts.set(id, chart);
}

export const CHART_IDS = ['cost', 'time', 'accuracy', 'tasks', 'skills'];

/**
 * Draws every .chart-card[data-chart] (or only `onlyId`) for the current language and theme.
 * `viewOptions` comes from the URL: `model` picks the per-task chart's model, `metric` every toggled chart's measure.
 */
export function renderCharts({ data, i18n, onlyId = null, viewOptions = {} }) {
  const models = measuredModels(data);
  if (!taskView.model || !models.some((model) => model.id === taskView.model)) taskView.model = models[0]?.id ?? null;
  if (viewOptions.model && models.some((model) => model.id === viewOptions.model)) taskView.model = viewOptions.model;
  if (TASK_METRICS.includes(viewOptions.metric)) Object.values(TOGGLED_CHARTS).forEach(({ view }) => { view.metric = viewOptions.metric; });
  const theme = readTheme([...SERIES, ...(data.skills?.arms ?? []).map((arm) => arm.id)]);
  window.Chart.defaults.font.family = theme.font;
  window.Chart.defaults.color = theme.muted;
  const context = { data, i18n, models, theme, isSolo: Boolean(onlyId) };
  document.querySelectorAll('.chart-card[data-chart]').forEach((card) => {
    if (!onlyId || card.dataset.chart === onlyId) drawChart(card, context);
  });
}
