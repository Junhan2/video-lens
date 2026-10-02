#!/usr/bin/env python3
"""Keeps docs/index.html and the translations in docs/i18n/ consistent with docs/i18n/en.json.

  python3 tools/i18n.py sync    write the English strings into index.html (text, aria-label, meta content),
                                so the page reads correctly before JavaScript loads, and one
                                <link rel="alternate" hreflang> per language in languages.json plus x-default
  python3 tools/i18n.py check   report problems; exit 1 on errors

check reports, per language listed in docs/i18n/languages.json:
  errors    keys that en.json does not have, placeholders ({name}) or markup (`code`, **bold**, [link](url))
            that differ from English, a listed language without its JSON file
  warnings  keys still missing (the page and READMEs fall back to English for those)
and for the page: data-i18n keys missing from en.json, inline English or hreflang links out of date (run sync),
string keys used literally in docs/assets/*.js that en.json lacks.
The numbers in the page's computed sentences come from tools/render_results.py, not from this script.
Standard library only.
"""
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
I18N = DOCS / "i18n"
PAGE = DOCS / "index.html"

RICH_TOKEN = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\))")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
TEXT_ELEMENT = re.compile(r'(<([a-zA-Z][\w-]*)\b[^>]*\sdata-i18n="([^"]+)"[^>]*>)(.*?)(</\2\s*>)', re.S)
ATTRIBUTE_TAG = re.compile(r'<[a-zA-Z][\w-]*\b[^>]*\sdata-i18n-(aria-label|content)="([^"]+)"[^>]*>')
HREFLANG_BLOCK = re.compile(r"(<!-- hreflang:start -->).*?(<!-- hreflang:end -->)", re.S)
CANONICAL = re.compile(r'<link rel="canonical" href="([^"]+)">')
SAFE_HREF = re.compile(r"^(https://|#|\.{0,2}/|[\w-]+\.(md|html)$)")   # isSafeHref in docs/assets/i18n.js
JS_KEY = re.compile(r"""['"]([a-z_]+(?:\.[a-z0-9_]+)+)['"]""")


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def flatten(tree, prefix=""):
    flat = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def rich_html(text):
    """The same three markup forms that docs/assets/i18n.js renders, as HTML."""
    out = []
    for part in RICH_TOKEN.split(text):
        if not part:
            continue
        if part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{html.escape(part[1:-1], quote=False)}</code>")
        elif part.startswith("**") and part.endswith("**"):
            out.append(f"<strong>{html.escape(part[2:-2], quote=False)}</strong>")
        elif part.startswith("["):
            label, href = re.match(r"^\[([^\]]+)\]\(([^)\s]+)\)$", part).groups()
            if SAFE_HREF.match(href):
                out.append(f'<a href="{html.escape(href)}">{html.escape(label, quote=False)}</a>')
            else:
                out.append(html.escape(label, quote=False))
        else:
            out.append(html.escape(part, quote=False))
    return "".join(out)


def set_attribute(tag, name, value):
    tag = re.sub(rf'\s{name}="[^"]*"', "", tag)
    return re.sub(r"^(<[a-zA-Z][\w-]*)", rf'\1 {name}="{html.escape(value)}"', tag, count=1)


def hreflang_links(page, manifest):
    """Each listed language at ?lang=<code> (the default language at the canonical URL), plus x-default."""
    base = CANONICAL.search(page).group(1)
    default = manifest.get("default", "en")
    links = [(language["code"], base if language["code"] == default else f"{base}?lang={language['code']}")
             for language in manifest["languages"]] + [("x-default", base)]
    return "".join(f'\n<link rel="alternate" hreflang="{code}" href="{html.escape(href)}">' for code, href in links)


def synced_page(page, strings, manifest):
    """index.html with every data-i18n element and attribute filled from en.json (unknown keys are left alone)
    and the hreflang links written from languages.json."""
    def fill_text(match):
        start, tag, key, _inner, end = match.groups()
        if key not in strings:
            return match.group(0)
        inner = html.escape(strings[key], quote=False) if tag.lower() == "title" else rich_html(strings[key])
        return f"{start}{inner}{end}"

    def fill_attribute(match):
        tag = match.group(0)
        for kind, key in re.findall(r'\sdata-i18n-(aria-label|content)="([^"]+)"', tag):
            if key in strings:
                tag = set_attribute(tag, kind, strings[key])
        return tag

    page = ATTRIBUTE_TAG.sub(fill_attribute, TEXT_ELEMENT.sub(fill_text, page))
    links = hreflang_links(page, manifest)
    return HREFLANG_BLOCK.sub(lambda match: f"{match.group(1)}{links}\n{match.group(2)}", page)


def markup_signature(text):
    return sorted(token[:1] if not token.startswith("[") else "[" + re.match(r"^\[[^\]]+\]\(([^)]+)\)$", token).group(1)
                  for token in RICH_TOKEN.findall(text))


def check_language(code, english, errors, warnings):
    path = I18N / f"{code}.json"
    if not path.exists():
        errors.append(f"{code}: listed in languages.json but {path.relative_to(ROOT)} does not exist")
        return
    strings = flatten(load_json(path))
    for key in sorted(set(strings) - set(english)):
        errors.append(f"{code}: {key} is not a key in en.json")
    missing = sorted(set(english) - set(strings))
    if missing:
        warnings.append(f"{code}: {len(missing)} keys fall back to English: {', '.join(missing[:8])}{' …' if len(missing) > 8 else ''}")
    for key in sorted(set(strings) & set(english)):
        if not isinstance(strings[key], str):
            errors.append(f"{code}: {key} must be a string")
            continue
        if set(PLACEHOLDER.findall(strings[key])) != set(PLACEHOLDER.findall(english[key])):
            errors.append(f"{code}: {key} placeholders {sorted(set(PLACEHOLDER.findall(strings[key])))} "
                          f"differ from English {sorted(set(PLACEHOLDER.findall(english[key])))}")
        if markup_signature(strings[key]) != markup_signature(english[key]):
            errors.append(f"{code}: {key} markup (`code`, **bold**, [link](url)) differs from English")


def check(english):
    errors, warnings = [], []
    sections = {key.split(".")[0] for key in english}
    page = PAGE.read_text(encoding="utf-8")
    page_keys = set(re.findall(r'data-i18n(?:-aria-label|-content)?="([^"]+)"', page))
    for key in sorted(page_keys - set(english)):
        errors.append(f"index.html: data-i18n key {key} is not in en.json")
    manifest = load_json(I18N / "languages.json")
    if synced_page(page, english, manifest) != page:
        errors.append("index.html: inline English or hreflang links out of date; run python3 tools/i18n.py sync")
    for script in sorted((DOCS / "assets").glob("*.js")):
        literals = {key for key in JS_KEY.findall(script.read_text(encoding="utf-8")) if key.split(".")[0] in sections}
        for key in sorted(literals - set(english)):
            errors.append(f"{script.relative_to(ROOT)}: key {key} is not in en.json")
    listed = [language["code"] for language in manifest["languages"]]
    if "en" not in listed:
        errors.append("languages.json: en must stay listed")
    for code in listed:
        if code != "en":
            check_language(code, english, errors, warnings)
    for path in sorted(I18N.glob("*.json")):
        if path.stem not in listed and path.name != "languages.json":
            warnings.append(f"{path.name}: not listed in languages.json, so the page does not offer it")
    return errors, warnings


def main(argv):
    command = argv[1] if len(argv) > 1 else "check"
    english = flatten(load_json(I18N / "en.json"))
    if command == "sync":
        page = PAGE.read_text(encoding="utf-8")
        updated = synced_page(page, english, load_json(I18N / "languages.json"))
        PAGE.write_text(updated, encoding="utf-8")
        print("index.html: " + ("updated from en.json" if updated != page else "already in sync"))
        return 0
    if command != "check":
        print(__doc__)
        return 2
    errors, warnings = check(english)
    for line in warnings:
        print(f"warning: {line}")
    for line in errors:
        print(f"error: {line}")
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
