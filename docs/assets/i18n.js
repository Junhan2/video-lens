// Loads UI strings for the page. English (i18n/en.json) is always loaded first and every other
// language is laid over it, so a key missing from a translation shows in English.
// String markup: `code`, **bold** and [text](url). Placeholders: {name}.

const I18N_DIR = 'i18n/';
export const DEFAULT_LANG = 'en';

// Browser codes that should land on a listed variant when no exact match exists.
const LANGUAGE_ALIASES = {
  'zh': 'zh-CN', 'zh-hans': 'zh-CN', 'zh-sg': 'zh-CN', 'zh-my': 'zh-CN',
  'zh-hant': 'zh-TW', 'zh-hk': 'zh-TW', 'zh-mo': 'zh-TW',
  'pt': 'pt-BR',
};

export async function fetchJson(path) {
  const response = await fetch(path, { cache: 'no-cache' });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

export async function loadLanguageList() {
  try {
    const manifest = await fetchJson(`${I18N_DIR}languages.json`);
    return manifest.languages.filter((language) => language && language.code);
  } catch {
    return [{ code: DEFAULT_LANG, name: 'English' }];
  }
}

/** ?lang= first, then the browser's languages, then English. Only listed codes are returned. */
export function pickLanguage(languages, requested, browserLanguages) {
  const byLower = new Map(languages.map((language) => [language.code.toLowerCase(), language.code]));
  const resolve = (code) => {
    if (!code) return null;
    const lower = code.toLowerCase();
    if (byLower.has(lower)) return byLower.get(lower);
    const script = lower.split('-').slice(0, 2).join('-');
    const alias = LANGUAGE_ALIASES[lower] || LANGUAGE_ALIASES[script] || LANGUAGE_ALIASES[lower.split('-')[0]];
    if (alias && byLower.has(alias.toLowerCase())) return byLower.get(alias.toLowerCase());
    const base = lower.split('-')[0];
    return byLower.get(base) || null;
  };
  for (const candidate of [requested, ...(browserLanguages || [])]) {
    const found = resolve(candidate);
    if (found) return found;
  }
  return DEFAULT_LANG;
}

function mergeDeep(base, overlay) {
  const merged = { ...base };
  for (const [key, value] of Object.entries(overlay || {})) {
    const isNested = value && typeof value === 'object' && !Array.isArray(value);
    merged[key] = isNested ? mergeDeep(base[key] || {}, value) : value;
  }
  return merged;
}

const stringFiles = new Map();

/**
 * One request per file per page view; English and the chosen language load in parallel.
 * A missing translation falls back to English; a missing en.json rejects, so the inline English stays.
 */
function fetchStrings(code) {
  if (!stringFiles.has(code)) {
    const request = fetchJson(`${I18N_DIR}${code}.json`);
    stringFiles.set(code, code === DEFAULT_LANG ? request : request.catch(() => ({})));
  }
  return stringFiles.get(code);
}

export async function loadStrings(code) {
  const [english, overlay] = await Promise.all([fetchStrings(DEFAULT_LANG), code === DEFAULT_LANG ? {} : fetchStrings(code)]);
  return mergeDeep(english, overlay);
}

function lookup(strings, key) {
  return key.split('.').reduce((node, part) => (node == null ? undefined : node[part]), strings);
}

function fill(template, params) {
  return template.replace(/\{(\w+)\}/g, (match, name) => (params && name in params ? String(params[name]) : match));
}

const RICH_TOKEN = /(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\))/g;

function isSafeHref(href) {
  return /^(https:\/\/|#|\.{0,2}\/|[\w-]+\.(md|html)$)/.test(href);
}

/** Replaces an element's content with the string, turning the three markup forms into nodes. */
function renderRich(element, text) {
  element.textContent = '';
  for (const part of text.split(RICH_TOKEN)) {
    if (!part) continue;
    if (part.startsWith('`') && part.endsWith('`')) {
      const code = document.createElement('code');
      code.textContent = part.slice(1, -1);
      element.append(code);
    } else if (part.startsWith('**') && part.endsWith('**')) {
      const strong = document.createElement('strong');
      strong.textContent = part.slice(2, -2);
      element.append(strong);
    } else if (part.startsWith('[')) {
      const [, label, href] = part.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      if (isSafeHref(href)) {
        const link = document.createElement('a');
        link.href = href;
        link.textContent = label;
        element.append(link);
      } else {
        element.append(label);
      }
    } else {
      element.append(part);
    }
  }
}

/** Number formatting for one locale. Tables keep the JSON's precision; chart labels round. */
function makeFormatters(code) {
  const cache = new Map();
  const formatter = (key, options) => {
    if (!cache.has(key)) cache.set(key, new Intl.NumberFormat(code, options));
    return cache.get(key);
  };
  const fixed = (digits) => ({ minimumFractionDigits: digits, maximumFractionDigits: digits });
  return {
    usd: (value, digits = 2) => formatter(`usd${digits}`, { style: 'currency', currency: 'USD', ...fixed(digits) }).format(value),
    seconds: (value, digits = 0) => formatter(`s${digits}`, { style: 'unit', unit: 'second', unitDisplay: 'narrow', ...fixed(digits) }).format(value),
    score: (value, digits = 3) => formatter(`n${digits}`, fixed(digits)).format(value),
    percent: (value) => formatter('pct', { style: 'percent', maximumFractionDigits: 0 }).format(value),
    integer: (value) => formatter('int', fixed(0)).format(value),
  };
}

export function makeTranslator(strings, code) {
  const t = (key, params) => {
    const value = lookup(strings, key);
    return typeof value === 'string' ? fill(value, params) : key;
  };
  const has = (key) => typeof lookup(strings, key) === 'string';
  const rich = (element, key, params) => renderRich(element, t(key, params));
  return { code, t, has, rich, format: makeFormatters(code) };
}

/** Writes every static string: text, aria-label and meta content, then <html lang> and the title. */
export function applyStaticStrings(i18n, root = document) {
  document.documentElement.lang = i18n.code;
  root.querySelectorAll('[data-i18n]').forEach((element) => {
    if (element.tagName === 'TITLE') element.textContent = i18n.t(element.dataset.i18n);
    else i18n.rich(element, element.dataset.i18n);
  });
  root.querySelectorAll('[data-i18n-aria-label]').forEach((element) => {
    element.setAttribute('aria-label', i18n.t(element.dataset.i18nAriaLabel));
  });
  root.querySelectorAll('[data-i18n-content]').forEach((element) => {
    element.setAttribute('content', i18n.t(element.dataset.i18nContent));
  });
}
