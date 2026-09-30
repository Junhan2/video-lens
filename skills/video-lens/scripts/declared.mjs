#!/usr/bin/env node
// video-lens declared timing (spec 7.9): the CSS animations and transitions a page declares, read from headless
// Chrome over the DevTools protocol with Node 24's built-in WebSocket (no npm).
//
//   node declared.mjs URL|FILE --out DIR [--viewport 1280x800] [--wait-ms 0] [--render FPS SECONDS]
//                    [--trigger "click|hover SELECTOR"]
//
// Writes DIR/declared.json, and DIR/render.mp4 with --render: every running CSS or Web Animations animation paused
// and seeked frame by frame, so the clip is a known answer for the measuring path (JS or canvas motion driven by
// requestAnimationFrame is not seekable and renders as it happens to be). Clip time 0 is the start of the first
// animation after --trigger, else of the first animation; each row's clip_start_ms is where its active phase begins.
// start_ms is document-timeline time. Prints nothing on success. On failure: one stderr line
// `video-lens: <what failed>. <what to do>`, exit 2 bad arguments, 3 page unreadable, 5 Node < 24 / Chrome / ffmpeg
// missing, 1 anything else. VIDEO_LENS_CHROME overrides the Chrome binary.
import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { once } from 'node:events';
import { constants, homedir, tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const EXIT_INTERNAL = 1;
const EXIT_BAD_ARGS = 2;
const EXIT_INPUT = 3;
const EXIT_DEPENDENCY = 5;
const HARD_KILL_MS = 60_000;
const KILL_MS_PER_FRAME = 500;          // renders measured ~57 ms per frame (480x240 to 1920x1080); 60 s alone would cut long ones
const PORT_WAIT_MS = 10_000;
const CHROME_ENV = 'VIDEO_LENS_CHROME';
const CHROME_APP = 'Google Chrome.app/Contents/MacOS/Google Chrome';
const CHROME_CANDIDATES = [join('/Applications', CHROME_APP), join(homedir(), 'Applications', CHROME_APP)];
const CHROME_FLAGS = ['--headless', '--remote-debugging-port=0', '--no-first-run', '--no-default-browser-check',
  '--hide-scrollbars', '--mute-audio', '--force-color-profile=srgb', '--disable-background-networking',
  '--disable-component-update', '--disable-sync', '--disable-default-apps', '--disable-crash-reporter'];
const PAGE_SCHEME = /^(https?|file|data|about):/i;
const TRIGGER_FORMAT = /^(click|hover)\s+(\S.*)$/s;
const VIEWPORT_FORMAT = /^(\d+)x(\d+)$/;
const CRF = '18';
const USAGE = `usage: node declared.mjs URL|FILE --out DIR [--viewport 1280x800] [--wait-ms 0]
                        [--render FPS SECONDS] [--trigger "click|hover SELECTOR"]
Writes DIR/declared.json (and DIR/render.mp4 with --render); on success prints its path and one line per
animation (at most 30): target, kind, properties, delay, duration, curve.
`;
const SUMMARY_MAX_ROWS = 30;

class Fail extends Error {
  constructor(code, what, todo = '') {
    super(what);
    this.code = code;
    this.todo = todo;
  }

  line() {
    return `video-lens: ${this.message.replace(/\.$/, '')}.${this.todo ? ` ${this.todo}` : ''}`;
  }
}

const badArgs = (what) => new Fail(EXIT_BAD_ARGS, what, 'Run node declared.mjs --help');

// ---------- arguments ----------

function parseCli(argv) {
  const args = { target: null, out: null, viewport: [1280, 800], waitMs: 0, render: null, trigger: null, isHelp: false };
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i];
    const value = () => {
      if (i + 1 >= argv.length) throw badArgs(`${flag} needs a value`);
      return argv[++i];
    };
    if (flag === '-h' || flag === '--help') args.isHelp = true;
    else if (flag === '--out') args.out = resolve(value());
    else if (flag === '--viewport') args.viewport = parseViewport(value());
    else if (flag === '--wait-ms') args.waitMs = parseNumber(value(), '--wait-ms', 0);
    else if (flag === '--render') args.render = parseRender(value(), value());
    else if (flag === '--trigger') args.trigger = parseTrigger(value());
    else if (flag.startsWith('-')) throw badArgs(`unknown option ${flag}`);
    else if (args.target === null) args.target = flag;
    else throw badArgs(`unexpected argument ${flag}`);
  }
  if (args.isHelp) return args;
  if (!args.target) throw badArgs('missing URL or HTML file');
  if (!args.out) throw badArgs('missing --out DIR');
  return args;
}

function parseNumber(text, label, minimum) {
  const number = Number(text);
  if (text === '' || !Number.isFinite(number) || number < minimum) throw badArgs(`${label} must be a number >= ${minimum}, got ${text}`);
  return number;
}

function parseViewport(text) {
  const match = VIEWPORT_FORMAT.exec(text);
  if (!match || Number(match[1]) < 1 || Number(match[2]) < 1) throw badArgs(`--viewport must look like 1280x800, got ${text}`);
  return [Number(match[1]), Number(match[2])];
}

function parseRender(fpsText, secondsText) {
  const fps = parseNumber(fpsText, '--render FPS', 0);
  const seconds = parseNumber(secondsText, '--render SECONDS', 0);
  const frames = Math.round(fps * seconds);
  if (frames < 1) throw badArgs(`--render ${fpsText} ${secondsText} gives no frames`);
  return { fps, seconds, frames };
}

function parseTrigger(text) {
  const match = TRIGGER_FORMAT.exec(text.trim());
  if (!match) throw badArgs(`--trigger must be "click SELECTOR" or "hover SELECTOR", got ${JSON.stringify(text)}`);
  return { action: match[1], selector: match[2].trim() };
}

function pageUrl(target) {
  if (PAGE_SCHEME.test(target)) return target;
  const path = resolve(target);
  if (!existsSync(path)) throw new Fail(EXIT_INPUT, `page file not found: ${path}`, 'Pass an http(s) URL or an existing HTML file');
  return pathToFileURL(path).href;
}

// ---------- dependencies ----------

function requireWebSocket() {
  if (typeof WebSocket !== 'function') {
    throw new Fail(EXIT_DEPENDENCY, `Node ${process.versions.node} has no built-in WebSocket`, 'Install Node 24 or newer');
  }
}

function findChrome() {
  const override = process.env[CHROME_ENV];
  const candidates = override ? [override] : CHROME_CANDIDATES;
  const found = candidates.find((path) => existsSync(path));
  if (!found) throw new Fail(EXIT_DEPENDENCY, `Google Chrome not found at ${candidates.join(' or ')}`, `Install Google Chrome or set ${CHROME_ENV} to its binary`);
  return found;
}

function requireFfmpeg() {
  if (spawnSync('ffmpeg', ['-version'], { stdio: 'ignore' }).error) {
    throw new Fail(EXIT_DEPENDENCY, 'ffmpeg not found (needed by --render)', 'Install ffmpeg or drop --render');
  }
}

// ---------- Chrome process ----------

// Only processes started with exactly this profile are killed (spec 7.9); the user's own Chrome is never touched.
function killProfileProcesses(profile) {
  const flag = `--user-data-dir=${profile}`;
  const lines = execFileSync('ps', ['-axww', '-o', 'pid=,command='], { encoding: 'utf8' }).split('\n');
  for (const line of lines) {
    const [pid, ...command] = line.trim().split(/\s+/);
    if (command.includes(flag) && Number(pid) !== process.pid) {
      try { process.kill(Number(pid), 'SIGKILL'); } catch { /* already gone */ }
    }
  }
}

function removeProfile(profile) {
  killProfileProcesses(profile);
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}

async function waitForDebugPort(profile, chrome) {
  const portFile = join(profile, 'DevToolsActivePort');
  const deadline = Date.now() + PORT_WAIT_MS;
  while (Date.now() < deadline) {
    if (chrome.exitCode !== null) throw new Fail(EXIT_DEPENDENCY, `Chrome exited with code ${chrome.exitCode} at startup`, `Check that ${chrome.spawnfile} starts`);
    const port = existsSync(portFile) ? readFileSync(portFile, 'utf8').split('\n')[0].trim() : '';
    if (port) return port;
    await sleep(50);
  }
  throw new Fail(EXIT_INTERNAL, `Chrome opened no debugging port within ${PORT_WAIT_MS / 1000} s`, 'Rerun; close stuck headless Chrome processes if it repeats');
}

async function pageSocketUrl(port) {
  const deadline = Date.now() + PORT_WAIT_MS;
  while (Date.now() < deadline) {
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = targets.find((target) => target.type === 'page');
    if (page) return page.webSocketDebuggerUrl;
    await sleep(50);
  }
  throw new Fail(EXIT_INTERNAL, 'Chrome listed no page target', 'Rerun');
}

async function launchChrome(binary, profile) {
  rmSync(profile, { recursive: true, force: true });
  const chrome = spawn(binary, [...CHROME_FLAGS, `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
  chrome.on('error', () => {});      // a spawn failure shows up as exitCode in waitForDebugPort
  const port = await waitForDebugPort(profile, chrome);
  const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  return { cdp: await Cdp.connect(await pageSocketUrl(port)), browser: version.Browser };
}

// ---------- DevTools protocol client ----------

class Cdp {
  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((opened, failed) => {
      socket.onopen = opened;
      socket.onerror = () => failed(new Fail(EXIT_INTERNAL, `cannot connect to Chrome at ${url}`, 'Rerun'));
    });
    return new Cdp(socket);
  }

  constructor(socket) {
    this.socket = socket;
    this.lastId = 0;
    this.calls = new Map();
    this.waiters = [];
    socket.onmessage = (event) => this.receive(JSON.parse(event.data));
    socket.onclose = () => {
      for (const call of this.calls.values()) call.rejected(new Fail(EXIT_INTERNAL, `Chrome closed the DevTools connection during ${call.method}`, 'Rerun'));
      this.calls.clear();
    };
  }

  send(method, params = {}) {
    return new Promise((resolved, rejected) => {
      const id = ++this.lastId;
      this.calls.set(id, { method, resolved, rejected });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  receive(message) {
    if (message.id !== undefined) {
      const call = this.calls.get(message.id);
      this.calls.delete(message.id);
      if (!call) return;
      if (message.error) call.rejected(new Error(`${call.method}: ${message.error.message}`));
      else call.resolved(message.result);
      return;
    }
    const ready = this.waiters.filter((waiter) => waiter.method === message.method);
    this.waiters = this.waiters.filter((waiter) => waiter.method !== message.method);
    for (const waiter of ready) waiter.resolved(message.params);
  }

  nextEvent(method) {
    return new Promise((resolved) => this.waiters.push({ method, resolved }));
  }

  // Runs `expression` in the page and returns its (awaited) value; a page exception becomes an Error.
  async evaluate(expression) {
    const { result, exceptionDetails } = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (exceptionDetails) throw new Error(exceptionDetails.exception?.description ?? exceptionDetails.text);
    return result.value;
  }

  close() {
    this.socket.close();
  }
}

// ---------- page side (serialised with toString, runs inside the page before its own scripts) ----------

function installRecorder() {
  const seen = new Set();
  const noteAnimations = () => { for (const animation of document.getAnimations()) seen.add(animation); };
  // Finished transitions vanish from getAnimations(), so they are caught when they start.
  document.addEventListener('transitionrun', noteAnimations, true);
  document.addEventListener('animationstart', noteAnimations, true);
  const KEYFRAME_META = new Set(['offset', 'computedOffset', 'easing', 'composite']);
  const MAX_CLASSES = 3;          // utility-class soup would make labels unreadable; three still select
  const round3 = (value) => Math.round(value * 1000) / 1000;
  // Scroll-driven timing is a CSSNumericValue such as 100%, which would serialise as {}.
  const timeValue = (value) => (typeof value === 'number' ? round3(value) : value == null ? null : String(value));
  const targetLabel = (element, pseudo) => {
    if (!element) return null;
    const id = element.id ? `#${element.id}` : '';
    const classes = [...element.classList].slice(0, MAX_CLASSES).map((name) => `.${name}`).join('');
    return `${element.localName}${id}${classes}${pseudo || ''}`;
  };
  // Document time at which the animation's local time was 0 (before its delay); null when it has none: paused by the
  // page, on a scroll timeline, or never played.
  const startOf = (animation, now) => {
    if (animation.timeline !== document.timeline || animation.playState === 'paused') return null;
    if (animation.startTime !== null) return animation.startTime;
    if (animation.currentTime === null) return null;
    return now - animation.currentTime / (animation.playbackRate || 1);
  };
  const describe = (animation, now) => {
    const effect = animation.effect;
    const computed = effect ? effect.getComputedTiming() : {};
    const keyframes = effect && effect.getKeyframes ? effect.getKeyframes() : [];
    return {
      target: targetLabel(effect && effect.target, effect && effect.pseudoElement),
      name: animation.animationName || animation.transitionProperty || animation.id || null,
      kind: animation.constructor.name,
      play_state: animation.playState,
      delay_ms: timeValue(computed.delay),
      duration_ms: timeValue(computed.duration),
      effect_easing: effect ? effect.getTiming().easing : null,
      keyframe_easings: keyframes.map((keyframe) => keyframe.easing),
      properties: [...new Set(keyframes.flatMap((keyframe) => Object.keys(keyframe).filter((key) => !KEYFRAME_META.has(key))))],
      iterations: computed.iterations === Infinity ? 'infinite' : computed.iterations,
      fill: computed.fill,
      start_ms: timeValue(startOf(animation, now)),
    };
  };
  let triggerAt = null;
  let driven = [];
  window.__videoLens = {
    markTrigger() { triggerAt = document.timeline.currentTime; },
    // Lists every animation seen or alive; with shouldPause also pauses the live ones and fixes the render origin:
    // the first animation that started after the trigger, else the first animation.
    async snapshot(shouldPause) {
      const alive = document.getAnimations();
      for (const animation of alive) seen.add(animation);
      const settled = Promise.all(alive.map((animation) => animation.ready.catch(() => null)));
      await Promise.race([settled, new Promise((done) => setTimeout(done, 1000))]);
      const now = document.timeline.currentTime;
      const liveNow = new Set(document.getAnimations());
      const rows = [...seen].map((animation) => ({ animation, row: describe(animation, now) }));
      if (!shouldPause) return { animations: rows.map(({ row }) => row), origin_ms: null };
      // Page-paused and scroll-driven animations have no start_ms and stay as the page left them.
      const seekable = rows.filter(({ animation, row }) => liveNow.has(animation) && row.start_ms !== null);
      const starts = seekable.map(({ row }) => row.start_ms);
      const afterTrigger = triggerAt === null ? [] : starts.filter((start) => start > triggerAt);
      const origin = Math.min(...(afterTrigger.length ? afterTrigger : starts));
      for (const { animation, row } of seekable) {
        animation.pause();
        row.clip_start_ms = round3(row.start_ms - origin + row.delay_ms);
      }
      driven = seekable.map(({ animation, row }) => ({ animation, offset: row.start_ms - origin }));
      return { animations: rows.map(({ row }) => row), origin_ms: driven.length ? round3(origin) : null, driven: driven.length };
    },
    // Puts every paused animation at clip time tMs, then waits two frames so the next screenshot shows it.
    seek(tMs) {
      for (const { animation, offset } of driven) animation.currentTime = (tMs - offset) * (animation.playbackRate || 1);
      return new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    },
  };
}

const TWO_FRAMES = 'new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))';

// ---------- steps ----------

async function openPage(cdp, url, [width, height]) {
  await cdp.send('Page.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
  await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: `(${installRecorder})()` });
  const loaded = cdp.nextEvent('Page.loadEventFired');
  const navigation = await cdp.send('Page.navigate', { url });
  if (navigation.errorText) throw new Fail(EXIT_INPUT, `cannot load ${url} (${navigation.errorText})`, 'Check the URL, or that the local server is running');
  await loaded;
  await cdp.evaluate(`document.fonts.ready.then(() => ${TWO_FRAMES})`);
}

async function triggerElement(cdp, { action, selector }) {
  const point = await cdp.evaluate(`(() => {
    let element;
    try { element = document.querySelector(${JSON.stringify(selector)}); } catch (error) { return { error: error.message }; }
    if (!element) return null;
    element.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    const box = element.getBoundingClientRect();
    return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
  })()`);
  if (!point || point.error) {
    throw new Fail(EXIT_BAD_ARGS, `--trigger selector ${selector} ${point ? `is invalid (${point.error})` : 'matches no element'}`, 'Check the selector against the page');
  }
  await cdp.evaluate('window.__videoLens.markTrigger()');
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: point.x, y: point.y });
  if (action === 'click') {
    await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', buttons: 1, clickCount: 1 });
    await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', buttons: 0, clickCount: 1 });
  }
  await cdp.evaluate(TWO_FRAMES);
}

// Encodes piped PNG screenshots into `path` (written as a hidden part file, renamed when complete).
function startEncoder(path, fps) {
  const partPath = join(dirname(path), `.${basename(path, '.mp4')}-${process.pid}.mp4`);
  const ffmpeg = spawn('ffmpeg', ['-v', 'error', '-y', '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'png', '-i', '-',
    '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-c:v', 'libx264', '-crf', CRF, '-pix_fmt', 'yuv420p', '-f', 'mp4', partPath],
  { stdio: ['pipe', 'ignore', 'pipe'] });
  let errorText = '';
  ffmpeg.stderr.on('data', (chunk) => { errorText += chunk; });
  ffmpeg.stdin.on('error', () => {});     // a dead ffmpeg is reported by its exit code in finish()
  const exited = once(ffmpeg, 'close');
  return {
    async write(png) {
      if (!ffmpeg.stdin.write(png)) await Promise.race([once(ffmpeg.stdin, 'drain'), exited]);
    },
    async finish() {
      ffmpeg.stdin.end();
      const [code] = await exited;
      if (code !== 0) throw new Fail(EXIT_INTERNAL, `ffmpeg could not encode render.mp4 (${errorText.trim().split('\n').pop()})`, 'Check ffmpeg with libx264');
      renameSync(partPath, path);
    },
    discard() {
      ffmpeg.kill('SIGKILL');
      rmSync(partPath, { force: true });
    },
  };
}

async function renderClip(cdp, outDir, { fps, frames }) {
  active.encoder = startEncoder(join(outDir, 'render.mp4'), fps);
  for (let index = 0; index < frames; index++) {
    await cdp.evaluate(`window.__videoLens.seek(${(index * 1000) / fps})`);
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png', optimizeForSpeed: true });
    await active.encoder.write(Buffer.from(shot.data, 'base64'));
  }
  await active.encoder.finish();
  active.encoder = null;
}

function byClipOrder(a, b) {
  const at = (row) => (row.start_ms === null ? Infinity : row.start_ms + (row.delay_ms || 0));
  return at(a) - at(b);
}

function writeJsonAtomic(path, document) {
  const part = `${path}.part`;
  writeFileSync(part, `${JSON.stringify(document, null, 1)}\n`);
  renameSync(part, path);
}

async function readDeclared(args, url, profile, binary) {
  const { cdp, browser } = await launchChrome(binary, profile);
  try {
    await openPage(cdp, url, args.viewport);
    if (args.trigger) await triggerElement(cdp, args.trigger);
    if (args.waitMs) await sleep(args.waitMs);
    const snapshot = await cdp.evaluate(`window.__videoLens.snapshot(${Boolean(args.render)})`);
    if (args.render) await renderClip(cdp, args.out, args.render);
    const render = args.render && { file: 'render.mp4', fps: args.render.fps, seconds: args.render.seconds,
      frames: args.render.frames, origin_ms: snapshot.origin_ms, animations_driven: snapshot.driven };
    return { schema: 'video-lens-declared/1', url, viewport: args.viewport, wait_ms: args.waitMs, trigger: args.trigger,
      chrome: browser, animations: snapshot.animations.sort(byClipOrder), render: render || null };
  } finally {
    cdp.close();
  }
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

// A CSS animation's curve sits in its keyframes (the effect easing reads linear); a transition's in the effect.
function curveOf(row) {
  if (row.effect_easing && row.effect_easing !== 'linear') return row.effect_easing;
  const keyframes = [...new Set((row.keyframe_easings || []).filter((easing) => easing && easing !== 'linear'))];
  return keyframes.length ? keyframes.join(' / ') : 'linear';
}

function summaryText(document, path) {
  const rows = document.animations;
  const lines = rows.slice(0, SUMMARY_MAX_ROWS).map((row) => [
    row.target, `${row.kind}${row.name ? ` ${row.name}` : ''}`, (row.properties || []).join(',') || '-',
    `delay ${row.delay_ms} ms`, `${row.duration_ms} ms`, curveOf(row),
    ...(row.iterations === 1 ? [] : [`iterations ${row.iterations}`]),
  ].join(' · '));
  if (rows.length > SUMMARY_MAX_ROWS) lines.push(`+${rows.length - SUMMARY_MAX_ROWS} more in declared.json`);
  const render = document.render ? ` · ${join(dirname(path), document.render.file)} (${document.render.frames} frames at ${document.render.fps} fps)` : '';
  return `${[`declared.json: ${path} · ${rows.length} animation(s)${render}`, ...lines].join('\n')}\n`;
}

// ---------- entry ----------

// What an abort must clean up: the Chrome profile (and its processes) and a half-written render.
const active = { profile: null, encoder: null };

function abort(fail) {
  active.encoder?.discard();
  if (active.profile) removeProfile(active.profile);
  process.stderr.write(`${fail.line()}\n`);
  process.exit(fail.code);
}

async function main() {
  const args = parseCli(process.argv.slice(2));
  if (args.isHelp) {
    process.stdout.write(USAGE);
    return;
  }
  requireWebSocket();
  const binary = findChrome();
  if (args.render) requireFfmpeg();
  const url = pageUrl(args.target);
  try {
    mkdirSync(args.out, { recursive: true });
  } catch (error) {
    throw badArgs(`cannot create --out ${args.out} (${error.code})`);
  }
  active.profile = join(tmpdir(), `vl-chrome-${process.pid}`);
  const budgetMs = HARD_KILL_MS + (args.render ? args.render.frames * KILL_MS_PER_FRAME : 0);
  const killer = setTimeout(() => abort(new Fail(EXIT_INTERNAL, `Chrome did not finish within ${budgetMs / 1000} s`, 'Check the page, or render fewer frames')), budgetMs);
  try {
    const path = resolve(args.out, 'declared.json');
    const document = await readDeclared(args, url, active.profile, binary);
    writeJsonAtomic(path, document);
    process.stdout.write(summaryText(document, path));
  } finally {
    clearTimeout(killer);
    active.encoder?.discard();
    active.encoder = null;
    removeProfile(active.profile);
    active.profile = null;
  }
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => abort(new Fail(128 + constants.signals[signal], `interrupted by ${signal}`, 'Rerun the same command')));
}

main().then(
  () => process.exit(0),
  (error) => abort(error instanceof Fail ? error : new Fail(EXIT_INTERNAL, `internal error (${error.message})`, 'Rerun; if it repeats, report the page')),
);
