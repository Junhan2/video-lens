#!/usr/bin/env python3
"""Make the Grok baseline (g0) load no skills, as --disable-slash-commands does for Claude.

Grok Build CLI also loads Claude plugins, ~/.claude/skills, ~/.agents/skills and its own skills, so the baseline
needs three switches:
  plugins   python3 grok_isolate.py plugins > <run folder>/.grok/config.toml   (every plugin Grok finds, disabled)
  skills    python3 grok_isolate.py skills >> ~/.grok/config.toml              (user scope only: [skills] ignore
                                                                                 ~/.agents/skills, disable the rest)
plus GROK_CLAUDE_SKILLS_ENABLED=false for ~/.claude/skills. run_final.sh grok appends the skills block for the g0
cells and restores ~/.grok/config.toml afterwards. Check a run with the first line of its log: it must list no skill.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

GROK_BIN = os.environ.get("GROK_BIN") or str(Path.home() / ".grok/bin/grok")
AGENTS_SKILLS = Path.home() / ".agents" / "skills"
BLOCK_MARK = "# video-lens benchmark g0 block"


def inspect():
    output = subprocess.run([GROK_BIN, "inspect", "--json"], capture_output=True, text=True, check=True,
                            env={**os.environ, "GROK_CLAUDE_SKILLS_ENABLED": "false"}).stdout
    return json.loads(output)


def toml_list(names):
    return "[" + ", ".join(json.dumps(name) for name in sorted(set(names))) + "]"


def main(mode):
    found = inspect()
    if mode == "plugins":
        print("[plugins]\ndisabled = " + toml_list(plugin["name"] for plugin in found["plugins"]))
    elif mode == "skills":
        # Built-in skills can be named with or without a "bundled:" prefix; list both forms.
        names = [form for skill in found["skills"]
                 for form in {skill["name"], skill["name"].split(":")[-1], f"bundled:{skill['name'].split(':')[-1]}"}
                 if skill["source"].get("type") == "bundled" or not form.startswith("bundled:")]
        print(f"\n{BLOCK_MARK}\n[skills]\nignore = {toml_list([str(AGENTS_SKILLS)])}\ndisabled = {toml_list(names)}")
    else:
        raise SystemExit("usage: grok_isolate.py plugins|skills")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
