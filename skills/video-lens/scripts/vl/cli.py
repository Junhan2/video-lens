"""Argument parser for every subcommand and flag of spec 6. Bad arguments raise VlError (exit 2)."""
import argparse
import re

from .context import Run, parse_time
from .errors import EXIT_BAD_ARGS, VlError

EVENT_ID_PATTERN = re.compile(r"^[Mm](\d+)$")
SEGMENT_ID_PATTERN = re.compile(r"^[Ss](\d+)$")


class Parser(argparse.ArgumentParser):
    """argparse prints usage and exits 2; video-lens wants one stderr line, so errors become VlError."""

    def error(self, message):
        raise VlError(EXIT_BAD_ARGS, f"{self.prog}: {message}", "Run vl.py <command> --help")


def time_arg(text):
    try:
        return parse_time(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a time: {text!r} (seconds or [hh:]mm:ss[.s])") from None


def time_list_arg(text):
    return [time_arg(part) for part in text.split(",") if part.strip()]


def roi_arg(text):
    try:
        x, y, w, h = (int(round(float(v))) for v in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"--roi wants x,y,w,h in pixels, got {text!r}") from None
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError(f"--roi needs x,y >= 0 and w,h > 0, got {text!r}")
    return (x, y, w, h)


def time_span_arg(text):
    """`A:B` with plain seconds, or `A-B` with any time format (`1:02-1:30`); times are never negative."""
    parts = text.split("-") if "-" in text else text.split(":") if text.count(":") == 1 else []
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"not a time span: {text!r} (A:B in seconds, or A-B such as 1:02-1:30)")
    start, end = time_arg(parts[0]), time_arg(parts[1])
    if end <= start:
        raise argparse.ArgumentTypeError(f"span end must be after its start: {text!r}")
    return (start, end)


def events_arg(text):
    """`M01,M03` and/or `2.0-6.5` -> [{"id": "M01"}, {"range": [2.0, 6.5]}]."""
    selection = []
    for part in (p.strip() for p in text.split(",") if p.strip()):
        match = EVENT_ID_PATTERN.match(part)
        selection.append({"id": f"M{int(match.group(1)):02d}"} if match else {"range": list(time_span_arg(part))})
    return selection


def id_arg(pattern, prefix):
    def parse(text):
        match = pattern.match(text)
        if not match:
            raise argparse.ArgumentTypeError(f"expected an id like {prefix}01, got {text!r}")
        return f"{prefix}{int(match.group(1)):02d}"
    return parse


def positive(kind):
    def parse(text):
        value = kind(text)
        if value <= 0:
            raise argparse.ArgumentTypeError(f"must be > 0: {text}")
        return value
    return parse


def auto_or(kind):
    def parse(text):
        return "auto" if text == "auto" else positive(kind)(text)
    return parse


def build_parser():
    parser = Parser(prog="vl.py", description="video-lens: local video analysis")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    add_analyze(commands)
    add_views(commands)
    probe = commands.add_parser("probe", help="metadata JSON only")
    probe.add_argument("input")
    selftest = commands.add_parser("selftest", help="known-answer tests; exit 1 on any FAIL")
    selftest.add_argument("--quick", action="store_true")
    selftest.add_argument("--only", type=lambda s: [p.strip() for p in s.split(",") if p.strip()])
    selftest.add_argument("--chrome", action="store_true")
    selftest.add_argument("--keep", action="store_true")
    cache = commands.add_parser("cache", help="list or purge ~/.cache/video-lens")
    action = cache.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true")
    action.add_argument("--purge-older", type=positive(float), metavar="DAYS")
    action.add_argument("--path", metavar="INPUT")
    return parser


def add_analyze(commands):
    analyze = commands.add_parser("analyze", help="measure a video; stdout is report.md")
    analyze.add_argument("input", help="video file or URL (downloaded with yt-dlp)")
    analyze.add_argument("--out", metavar="DIR")
    general = analyze.add_argument_group("general")
    general.add_argument("--mode", choices=("auto", "content", "motion", "both"), default="auto")
    general.add_argument("--start", type=time_arg)
    general.add_argument("--end", type=time_arg)
    general.add_argument("--roi", type=roi_arg, metavar="x,y,w,h")
    general.add_argument("--refresh", action="store_true")
    general.add_argument("--json", action="store_true")
    general.add_argument("--budget-chars", type=positive(int), default=6000)
    general.add_argument("--progress", action="store_true")
    speech = analyze.add_argument_group("speech")
    speech.add_argument("--lang", default="ko-KR", help="BCP 47 locale, or none")
    speech.add_argument("--asr", choices=("auto", "apple", "whisper", "none"), default="auto")
    speech.add_argument("--whisper-model", metavar="PATH")
    content = analyze.add_argument_group("content")
    content.add_argument("--ocr", choices=("auto", "on", "off"), default="auto")
    content.add_argument("--ocr-langs", default="ko-KR,en-US")
    content.add_argument("--ocr-max", type=positive(int), default=900)
    content.add_argument("--jobs", type=positive(int), default=4)
    content.add_argument("--max-keyframes", type=auto_or(int), default="auto")
    content.add_argument("--cell", type=auto_or(float), default="auto")
    content.add_argument("--pix", type=positive(int), default=20)
    content.add_argument("--cut-frac", type=positive(float), default=0.30)
    content.add_argument("--cut-ratio", type=positive(float), default=3.0)
    content.add_argument("--key-frac", type=positive(float), default=0.04)
    content.add_argument("--key-low", type=positive(float), default=0.002)
    content.add_argument("--ocr-frac", type=positive(float), default=0.002)
    content.add_argument("--fast", choices=("auto", "on", "off"), default="auto")
    content.add_argument("--timeline-chars", type=positive(int), default=40000)
    motion = analyze.add_argument_group("motion")
    motion.add_argument("--dpr", type=positive(float), default=1.0)
    motion.add_argument("--events", type=events_arg, metavar="LIST")
    motion.add_argument("--motion-mode", choices=("auto", "elements", "scroll"), default="auto")
    motion.add_argument("--det-side", type=positive(int), default=640)
    motion.add_argument("--work-side", type=int, default=1280, help="0 = native")
    motion.add_argument("--pix-threshold", type=positive(int), default=8)
    motion.add_argument("--gap-ms", type=positive(float), default=150.0)
    motion.add_argument("--noise-maxd", type=positive(int), default=32)
    motion.add_argument("--max-elements", type=positive(int), default=24)
    motion.add_argument("--micro", action="store_true")
    motion.add_argument("--no-joint", action="store_true")
    motion.add_argument("--sheets", type=int, default=6, help="0 = none")
    motion.add_argument("--easings", metavar="FILE")


def add_views(commands):
    zoom = commands.add_parser("zoom", help="denser sheet of one event, segment or time range")
    zoom.add_argument("out")
    target = zoom.add_mutually_exclusive_group(required=True)
    target.add_argument("--event", type=id_arg(EVENT_ID_PATTERN, "M"))
    target.add_argument("--segment", type=id_arg(SEGMENT_ID_PATTERN, "S"))
    target.add_argument("--range", type=time_span_arg, metavar="A:B")
    zoom.add_argument("--cells", type=positive(int), default=12)
    frame = commands.add_parser("frame", help="native crop PNG(s) at exact times")
    frame.add_argument("out")
    frame.add_argument("--t", type=time_list_arg, required=True, metavar="T[,T...]")
    frame.add_argument("--roi", type=roi_arg, metavar="x,y,w,h")
    frame.add_argument("--width", type=positive(int), metavar="W", help="downsize only")
    text = commands.add_parser("text", help="timeline rows: speech and on-screen text")
    text.add_argument("out")
    text.add_argument("--from", dest="start", type=time_arg, metavar="T")
    text.add_argument("--to", dest="end", type=time_arg, metavar="T")
    text.add_argument("--grep", metavar="REGEX")
    text.add_argument("--kind", choices=("say", "ocr", "all"), default="all")
    text.add_argument("--max-lines", type=positive(int), default=200)
    rows = commands.add_parser("rows", help="table rows that did not fit the report")
    rows.add_argument("out")
    rows.add_argument("--kind", required=True, choices=("motion", "elements", "events", "keyframes", "text", "shots"))
    rows.add_argument("--from", dest="start", type=positive(int), default=1, metavar="N")
    rows.add_argument("--count", type=positive(int), default=30)
    digest = commands.add_parser("digest", help="on request only: one capture per scene, then notes from captions")
    digest.add_argument("out")
    step = digest.add_mutually_exclusive_group()
    step.add_argument("--scenes", type=positive(int), metavar="N",
                      help="at most N scenes (default: the chapters, else 6 + 2 per 10 min, at most 30)")
    step.add_argument("--captions", metavar="FILE", help='{"S01": "...", ...}: writes digest.md and digest.html')


def parse_args(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "analyze" and args.start is not None and args.end is not None and args.end <= args.start:
        raise VlError(EXIT_BAD_ARGS, f"--end {args.end:g} must be after --start {args.start:g}", "Fix the range")
    return args


def prepare_run(argv):
    """`Run` exactly as `vl.py analyze ARGV...` builds it before its stages: probe, cache, OUT, range, params.

    For module builders and KATs: prepare_run(["analyze", video, "--out", out_dir, "--mode", "motion"]).
    """
    args = parse_args(argv)
    if args.command != "analyze":
        raise VlError(EXIT_BAD_ARGS, "prepare_run takes analyze arguments", "Start argv with 'analyze'")
    return Run.open_for_analyze(args)
