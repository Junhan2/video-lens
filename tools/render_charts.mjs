#!/usr/bin/env node
// Renders the benchmark charts of docs/index.html to PNG with headless Chrome, light and dark,
// at device scale 2: docs/assets/charts/<id>-light.png and <id>-dark.png.
// Uses only Node 24 built-ins (http server, global WebSocket for the DevTools protocol) and a local Chrome.
//
//   node tools/render_charts.mjs                 all charts, both themes
//   node tools/render_charts.mjs --only cost     one chart
//   node tools/render_charts.mjs --page out.png [--width 1280] [--theme dark] [--lang en]
//                                                a full-page screenshot, for checking the layout
// Chrome path: $CHROME_PATH, else the default macOS location. The page loads Chart.js from cdnjs,
// so this needs network access.

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DOCS = join(ROOT, 'docs');
const OUT_DIR = join(DOCS, 'assets', 'charts');
const CHROME = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const CHARTS = [
  { id: 'cost', query: '' },
  { id: 'time', query: '' },
  { id: 'accuracy', query: '' },
  { id: 'tasks', query: '&metric=time' },
];
const THEMES = ['light', 'dark'];
const SCALE = 2;
const TIMEOUT_MS = 30000;
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml', '.md': 'text/markdown' };

function parseArgs(argv) {
  const args = { only: null, page: null, width: 1280, theme: 'light', lang: 'en' };
  for (let i = 0; i < argv.length; i += 1) {
    const [flag, value] = [argv[i], argv[i + 1]];
    if (flag === '--only') args.only = value;
    else if (flag === '--page') args.page = value;
    else if (flag === '--width') args.width = Number(value);
    else if (flag === '--theme') args.theme = value;
    else if (flag === '--lang') args.lang = value;
    else throw new Error(`unknown argument: ${flag}`);
    i += 1;
  }
  return args;
}

/** Serves docs/ on 127.0.0.1 so the page can fetch its JSON (file:// would block it). */
function startServer() {
  const server = createServer(async (request, response) => {
    const path = normalize(decodeURIComponent(new URL(request.url, 'http://x').pathname)).replace(/^(\.\.[/\\])+/, '');
    const file = join(DOCS, path.endsWith('/') ? `${path}index.html` : path);
    if (!file.startsWith(DOCS)) {
      response.writeHead(403).end();
      return;
    }
    try {
      const body = await readFile(file);
      response.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream' }).end(body);
    } catch {
      response.writeHead(404).end();
    }
  });
  return new Promise((done) => server.listen(0, '127.0.0.1', () => done(server)));
}

async function waitFor(check, what, timeout = TIMEOUT_MS) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const value = await check();
    if (value) return value;
    await new Promise((next) => setTimeout(next, 100));
  }
  throw new Error(`timed out after ${timeout} ms waiting for ${what}`);
}

async function launchChrome() {
  if (!existsSync(CHROME)) throw new Error(`Chrome not found at ${CHROME}; set CHROME_PATH`);
  const profile = await mkdtemp(join(tmpdir(), 'vl-render-'));
  const chrome = spawn(CHROME, [
    '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run',
    '--no-default-browser-check', '--hide-scrollbars', '--force-color-profile=srgb', 'about:blank',
  ], { stdio: 'ignore' });
  const portFile = join(profile, 'DevToolsActivePort');
  const port = await waitFor(async () => (existsSync(portFile) ? (await readFile(portFile, 'utf8')).split('\n')[0] : null), 'Chrome to start');
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json();
  return { chrome, profile, socketUrl: target.webSocketDebuggerUrl };
}

/** A minimal DevTools protocol client over the built-in WebSocket. */
async function connect(socketUrl) {
  const socket = new WebSocket(socketUrl);
  await new Promise((done, fail) => {
    socket.addEventListener('open', done, { once: true });
    socket.addEventListener('error', fail, { once: true });
  });
  let nextId = 0;
  const pending = new Map();
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (!pending.has(message.id)) return;
    const { done, fail } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) fail(new Error(`${message.error.message} (${message.error.code})`));
    else done(message.result);
  });
  const send = (method, params = {}) => new Promise((done, fail) => {
    nextId += 1;
    pending.set(nextId, { done, fail });
    socket.send(JSON.stringify({ id: nextId, method, params }));
  });
  const evaluate = async (expression) => (await send('Runtime.evaluate', { expression, returnByValue: true })).result.value;
  return { send, evaluate, close: () => socket.close() };
}

async function openPage(cdp, url, { width, height, theme }) {
  await cdp.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: SCALE, mobile: false });
  await cdp.send('Emulation.setEmulatedMedia', {
    features: [{ name: 'prefers-color-scheme', value: theme }, { name: 'prefers-reduced-motion', value: 'reduce' }],
  });
  await cdp.send('Page.navigate', { url });
  await waitFor(() => cdp.evaluate('document.readyState === "complete"'), `${url} to load`);
}

async function renderChart(cdp, base, chart, theme) {
  const url = `${base}/index.html?chart=${chart.id}&theme=${theme}&lang=en${chart.query}`;
  // Transparent outside the card, so the image sits on any README background (GitHub uses #fff and #0d1117).
  await cdp.send('Emulation.setDefaultBackgroundColorOverride', { color: { r: 0, g: 0, b: 0, a: 0 } });
  await openPage(cdp, url, { width: 900, height: 1400, theme });
  await waitFor(() => cdp.evaluate('document.documentElement.dataset.ready === "true"'), `chart ${chart.id} (${theme})`);
  const box = await cdp.evaluate('(() => { const r = document.getElementById("solo").getBoundingClientRect(); return { x: r.x, y: r.y, width: Math.ceil(r.width), height: Math.ceil(r.height) }; })()');
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png', clip: { ...box, scale: 1 }, captureBeyondViewport: true });
  const file = join(OUT_DIR, `${chart.id}-${theme}.png`);
  await writeFile(file, Buffer.from(shot.data, 'base64'));
  return { file, width: box.width * SCALE, height: box.height * SCALE };
}

async function renderPage(cdp, base, args) {
  const url = `${base}/index.html?theme=${args.theme}&lang=${args.lang}`;
  await openPage(cdp, url, { width: args.width, height: 1000, theme: args.theme });
  await waitFor(() => cdp.evaluate('document.querySelectorAll(".chart-card canvas").length >= 4 || !!document.querySelector(".chart-card .chart-loading")'), 'charts');
  await new Promise((next) => setTimeout(next, 500));
  const height = await cdp.evaluate('document.documentElement.scrollHeight');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: args.width, height, deviceScaleFactor: 1, mobile: false });
  await new Promise((next) => setTimeout(next, 500));
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  await writeFile(args.page, Buffer.from(shot.data, 'base64'));
  return { file: args.page, width: args.width, height };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const server = await startServer();
  const base = `http://127.0.0.1:${server.address().port}`;
  const { chrome, profile, socketUrl } = await launchChrome();
  const cdp = await connect(socketUrl);
  try {
    if (args.page) {
      const result = await renderPage(cdp, base, args);
      console.log(`${result.file} ${result.width}x${result.height}`);
      return;
    }
    await mkdir(OUT_DIR, { recursive: true });
    for (const chart of CHARTS.filter((entry) => !args.only || entry.id === args.only)) {
      for (const theme of THEMES) {
        const result = await renderChart(cdp, base, chart, theme);
        console.log(`${result.file.replace(`${ROOT}/`, '')} ${result.width}x${result.height}`);
      }
    }
  } finally {
    cdp.close();
    const exited = new Promise((done) => chrome.once('exit', done));
    chrome.kill();
    await exited;
    server.close();
    await rm(profile, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`render_charts: ${error.message}`);
  process.exit(1);
});
