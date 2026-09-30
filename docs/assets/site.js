// Page wiring: language, computed sentences, copy buttons, the hero frame strip and the charts.
// ?lang=<code> picks a language (else the browser's), ?theme=light|dark forces a theme,
// ?chart=<id> shows one chart alone at a fixed size for screenshots (tools/render_charts.mjs).

import { DEFAULT_LANG, applyStaticStrings, loadLanguageList, loadStrings, makeTranslator, pickLanguage } from './i18n.js';
import { CHART_IDS, describeChange, formatDate, measuredModels, renderCharts, taskLabel } from './charts.js';

const params = new URLSearchParams(window.location.search);
const soloChart = CHART_IDS.includes(params.get('chart')) ? params.get('chart') : null;
const state = { data: null, i18n: null, languages: [], hasDataError: false };

async function loadData() {
  try {
    const response = await fetch('data/benchmark.json', { cache: 'no-cache' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } catch {
    state.hasDataError = true;
    return null;
  }
}

function setRich(id, key, values) {
  const element = document.getElementById(id);
  if (element) state.i18n.rich(element, key, values);
}

/** Replaces a list's items with one rich string per [key, values] pair. */
function fillList(id, entries) {
  const list = document.getElementById(id);
  if (!list) return;
  list.textContent = '';
  entries.forEach(([key, values]) => {
    const item = document.createElement('li');
    state.i18n.rich(item, key, values);
    list.append(item);
  });
}

/** Values for setup.line: the benchmark-wide settings overlaid with the model's own (harness, effort, cost, time). */
function setupValues(i18n, data, model) {
  const settings = { ...data.settings, ...model.settings };
  const { harness } = settings;
  return {
    model: model.label, harness, effort: settings.effort,
    cost: i18n.t(`setup.cost_${settings.cost}`, { harness }),
    time: i18n.t(`setup.time_${settings.time}`, { harness }),
  };
}

/** Sentences whose numbers come from benchmark.json. */
function fillComputedText() {
  const { data, i18n } = state;
  const fmt = i18n.format;
  const models = measuredModels(data);
  const lead = models[0];
  if (!lead) return;
  const counts = { tasks: fmt.integer(data.tasks_count), reps: fmt.integer(data.runs_per_task), runs: fmt.integer(data.runs_per_condition) };
  const overallChange = (model, field) => describeChange(i18n, model.overall.baseline[field], model.overall.skill[field]);

  setRich('hero-summary', 'hero.summary', counts);
  fillList('hero-models', models.map((model) => ['hero.summary_model', {
    model: model.label, cost: overallChange(model, 'cost_usd'), time: overallChange(model, 'time_s'),
    skill_mean: fmt.score(model.overall.skill.mean_score), base_mean: fmt.score(model.overall.baseline.mean_score),
  }]));

  const spread = models
    .flatMap((model) => data.tasks.map((task) => ({ model, task, cell: model.per_task[task.id]?.baseline })))
    .filter((entry) => entry.cell)
    .sort((a, b) => a.cell.worst_score - b.cell.worst_score)[0];
  if (spread) {
    setRich('why-spread', 'why.alone_spread', {
      task: taskLabel(i18n, spread.task, 'short'), model: spread.model.label, reps: counts.reps,
      median: fmt.score(spread.cell.median_score), worst: fmt.score(spread.cell.worst_score),
    });
  }

  fillList('why-models', models.map((model) => ['why.alone_model', {
    model: model.label, cost: overallChange(model, 'cost_usd'), time: overallChange(model, 'time_s'),
    skill_mean: fmt.score(model.overall.skill.mean_score), base_mean: fmt.score(model.overall.baseline.mean_score),
    skill_worst: fmt.score(model.overall.skill.worst_score), base_worst: fmt.score(model.overall.baseline.worst_score),
  }]));

  setRich('bench-intro', 'bench.intro', { ...counts, date: formatDate(i18n.code, data.measured_on) });

  const lecture = lead.per_task.h3;
  if (lecture) {
    setRich('when-long', 'when.use_long', {
      model: lead.label, reps: counts.reps,
      cost: describeChange(i18n, lecture.baseline.median_cost_usd, lecture.skill.median_cost_usd),
      time: describeChange(i18n, lecture.baseline.median_time_s, lecture.skill.median_time_s),
    });
  }

  fillList('method-setup', models.map((model) => ['setup.line', setupValues(i18n, data, model)]));
  setRich('method-tasks-title', 'method.tasks_title', counts);
  const body = document.getElementById('method-tasks');
  body.textContent = '';
  data.tasks.forEach((task) => {
    const row = document.createElement('tr');
    const id = document.createElement('th');
    id.scope = 'row';
    id.textContent = task.id;
    const label = document.createElement('td');
    label.textContent = taskLabel(i18n, task, 'long');
    row.append(id, label);
    body.append(row);
  });
}

function fillVersion() {
  const footer = document.getElementById('footer-version');
  state.i18n.rich(footer, 'footer.version', { version: footer.dataset.version });
}

function showChartError(key) {
  document.querySelectorAll('.chart-card[data-chart]').forEach((card) => {
    card.textContent = '';
    const note = document.createElement('p');
    note.className = 'chart-loading';
    state.i18n.rich(note, key);
    card.append(note);
  });
}

function drawCharts() {
  if (state.hasDataError) {
    showChartError('chart.load_error');
    return;
  }
  if (!window.Chart) {
    showChartError('chart.library_error');
    return;
  }
  renderCharts({
    data: state.data,
    i18n: state.i18n,
    onlyId: soloChart,
    taskOptions: { model: params.get('model'), metric: params.get('metric') },
  });
}

/** Each language's page is its own canonical URL, so the hreflang links in the head can point at it. */
function updateCanonical(code) {
  const link = document.querySelector('link[rel="canonical"]');
  if (!link) return;
  if (!link.dataset.base) link.dataset.base = link.href;
  link.href = code === DEFAULT_LANG ? link.dataset.base : `${link.dataset.base}?lang=${encodeURIComponent(code)}`;
}

async function applyLanguage(code) {
  const strings = await loadStrings(code);
  state.i18n = makeTranslator(strings, code);
  applyStaticStrings(state.i18n);
  updateCanonical(code);
  fillVersion();
  if (state.data) fillComputedText();
  drawCharts();
}

function buildLanguageSwitcher(current) {
  const select = document.getElementById('lang-select');
  select.textContent = '';
  state.languages.forEach((language) => {
    const option = document.createElement('option');
    option.value = language.code;
    option.lang = language.code;
    option.textContent = language.name;
    option.selected = language.code === current;
    select.append(option);
  });
  select.addEventListener('change', async () => {
    params.set('lang', select.value);
    window.history.replaceState(null, '', `${window.location.pathname}?${params}${window.location.hash}`);
    await applyLanguage(select.value);
  });
}

/** The buttons' aria-label stays fixed, so the result is announced through a live region. */
function setupCopyButtons() {
  const status = document.getElementById('copy-status');
  const report = (button, key) => {
    button.textContent = state.i18n.t(key);
    if (status) status.textContent = state.i18n.t(key);
  };
  document.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', async () => {
      const source = document.getElementById(button.dataset.copy);
      const reset = () => {
        button.textContent = state.i18n.t('install.copy');
        if (status) status.textContent = '';
      };
      try {
        await navigator.clipboard.writeText(source.textContent);
        report(button, 'install.copied');
      } catch {
        const range = document.createRange();
        range.selectNodeContents(source);
        window.getSelection().removeAllRanges();
        window.getSelection().addRange(range);
        report(button, 'install.selected');
      }
      window.setTimeout(reset, 1600);
    });
  });
}

/** CSS cubic-bezier(x1, y1, x2, y2) as a function of progress, solved by bisection. */
function cubicBezier(x1, y1, x2, y2) {
  const curve = (a, b, t) => 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3;
  return (x) => {
    let [low, high] = [0, 1];
    for (let i = 0; i < 40; i += 1) {
      const middle = (low + high) / 2;
      if (curve(x1, x2, middle) < x) low = middle;
      else high = middle;
    }
    return curve(y1, y2, (low + high) / 2);
  };
}

/**
 * Hero strip: 30 frames at 60 fps (500 ms). A toast slides in over frames 6 to 23 (300 ms) on
 * cubic-bezier(0.22, 1, 0.36, 1). The sampled row keeps only frame 0: at 2 fps the next sample
 * is frame 30, so none of the 18 animation frames is seen.
 */
function drawFrameStrip() {
  const FRAMES = 30;
  const START = 6;
  const LENGTH = 18;
  const [CELL_W, CELL_H, GAP] = [16, 26, 3];
  const ease = cubicBezier(0.22, 1, 0.36, 1);
  const ns = 'http://www.w3.org/2000/svg';
  const make = (tag, attributes, parent) => {
    const element = document.createElementNS(ns, tag);
    Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, value));
    parent.append(element);
    return element;
  };
  document.querySelectorAll('[data-strip]').forEach((holder) => {
    const isSampled = holder.dataset.strip === 'sampled';
    const svg = make('svg', { viewBox: `0 0 ${FRAMES * (CELL_W + GAP) - GAP} ${CELL_H + 7}`, 'aria-hidden': 'true', focusable: 'false' }, holder);
    for (let frame = 0; frame < FRAMES; frame += 1) {
      const x = frame * (CELL_W + GAP);
      const isSeen = !isSampled || frame === 0;
      const isAnimating = frame >= START && frame < START + LENGTH;
      make('rect', { x, y: 0, width: CELL_W, height: CELL_H, rx: 2, class: isSeen ? 'cell' : 'cell unseen' }, svg);
      if (isSeen) {
        const frameView = make('svg', { x, y: 0, width: CELL_W, height: CELL_H, viewBox: `0 0 ${CELL_W} ${CELL_H}` }, svg);
        const progress = Math.min(Math.max((frame - START) / LENGTH, 0), 1);
        const top = CELL_H + 1 + (CELL_H - 8 - (CELL_H + 1)) * ease(progress);
        make('rect', { x: 2, y: top, width: CELL_W - 4, height: 5, rx: 1.5, class: 'toast' }, frameView);
      }
      if (isAnimating) make('rect', { x, y: CELL_H + 4, width: CELL_W, height: 3, rx: 1, class: isSampled ? 'span missed' : 'span' }, svg);
    }
  });
}

function watchTheme() {
  const query = window.matchMedia('(prefers-color-scheme: dark)');
  query.addEventListener('change', () => {
    if (!document.documentElement.dataset.theme && state.i18n) drawCharts();
  });
}

function enterSolo(id) {
  document.documentElement.classList.add('solo');
  const holder = document.createElement('div');
  holder.id = 'solo';
  holder.append(document.querySelector(`.chart-card[data-chart="${id}"]`));
  document.body.prepend(holder);
}

async function markReady() {
  await document.fonts.ready;
  window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
    document.documentElement.dataset.ready = 'true';
  }));
}

async function main() {
  if (soloChart) enterSolo(soloChart);
  drawFrameStrip();
  const [languages, data] = await Promise.all([loadLanguageList(), loadData()]);
  state.languages = languages;
  state.data = data;
  const code = pickLanguage(languages, params.get('lang'), navigator.languages || [navigator.language]);
  await applyLanguage(code);
  buildLanguageSwitcher(code);
  setupCopyButtons();
  watchTheme();
  if (soloChart) markReady();
}

main();
