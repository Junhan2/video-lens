#!/usr/bin/env python3
"""video-lens entry: runs one subcommand and maps every failure to the exit codes of spec 5.3.

stdout carries only the command's result (report.md for analyze); stderr stays silent except one error line.
Stage modules are imported late (`stage()`), so each command needs only the modules it uses.
"""
import importlib
import json
import sys
import tempfile
import traceback
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vl import SELFTEST_DIR, cache, decode, views  # noqa: E402
from vl.cli import parse_args  # noqa: E402
from vl.context import (AUTO_ACTIVITY_MAX, AUTO_MOTION_MAX_S, CONTENT_MODES, MOTION_MODES, Run,  # noqa: E402
                        resolve_input, validated_roi)
from vl.errors import EXIT_DEPENDENCY, EXIT_OK, VlError  # noqa: E402
from vl.probe import probe  # noqa: E402

LOG_DIR = Path(tempfile.gettempdir()) / "video-lens"


def stage(module, function):
    """The named function of scripts/vl/<module>.py; a missing module is a broken install (exit 5)."""
    try:
        loaded = importlib.import_module(f"vl.{module}")
    except ModuleNotFoundError as error:
        if error.name != f"vl.{module}":
            raise
        raise VlError(EXIT_DEPENDENCY, f"module scripts/vl/{module}.py is missing", "Reinstall the video-lens skill") from None
    return getattr(loaded, function)


def choose_mode(requested, range_s, audio):
    """Spec 7.2: <= 120 s and quiet or silent -> motion; > 120 s -> content; otherwise both."""
    if requested != "auto":
        return requested, f"--mode {requested}"
    if range_s > AUTO_MOTION_MAX_S:
        return "content", f"range {range_s:.1f} s > {AUTO_MOTION_MAX_S:.0f} s"
    short = f"range {range_s:.1f} s <= {AUTO_MOTION_MAX_S:.0f} s and"
    if audio is None:
        return "motion", f"{short} no audio"
    ratio = audio["activity_ratio"]
    if ratio < AUTO_ACTIVITY_MAX:
        return "motion", f"{short} audio activity {ratio:.2f} < {AUTO_ACTIVITY_MAX}"
    return "both", f"{short} audio activity {ratio:.2f} >= {AUTO_ACTIVITY_MAX}"


def is_speech_on(speech_params):
    return speech_params["lang"] != "none" and speech_params["asr"] != "none"


def run_stages(run):
    """Stage order: audio (mode selection needs it), speech, content, motion, sync, views."""
    if run.probe["audio_stream"] is not None:
        with run.timer("audio"):
            run.analysis["audio"] = stage("audio", "analyze_audio")(run)
    run.set_mode(*choose_mode(run.args.mode, run.range.duration_s, run.analysis["audio"]))
    mode = run.analysis["mode"]
    if mode in CONTENT_MODES and is_speech_on(run.params["speech"]):
        with run.timer("speech"):
            run.analysis["speech"] = stage("speech", "analyze_speech")(run)
    if mode in CONTENT_MODES:
        run.analysis["content"] = stage("survey", "analyze_content")(run)
    if mode in MOTION_MODES:
        run.analysis["motion"] = stage("detect", "analyze_motion")(run)
    if run.analysis["audio"] is not None:
        with run.timer("sync"):
            run.analysis["sync"] = stage("sync", "analyze_sync")(run)
    with run.timer("views"):
        stage("sheets", "render_views")(run)


def cmd_analyze(args):
    run = Run.open_for_analyze(args)
    with run.cache.locked():
        cache.enforce_lru(run.cache.root, keep=[run.cache.dir, run.input_path])
        run.cache.touch()
        run_stages(run)
        run.finish_timing()
        report = stage("report", "render_report")(run)
        analysis_text = run.write_analysis()
    cache.write_atomic(run.out / "report.md", report.encode())
    sys.stdout.write(analysis_text if args.json else report)
    return EXIT_OK


def cmd_probe(args):
    path, url = resolve_input(args.input)
    info, _ = probe(path)
    document = {"input": {"path": info["path"], "url": url, "key": cache.file_key(path), "size_bytes": info["size_bytes"]}}
    document.update({k: info[k] for k in ("duration_s", "start_time_s", "video", "audio_stream", "subtitle_streams", "sidecar_subtitles")})
    sys.stdout.write(json.dumps(document, ensure_ascii=False, indent=1) + "\n")
    return EXIT_OK


def cmd_cache(args):
    root = cache.cache_root()
    if args.path:
        path, _ = resolve_input(args.path)
        print(root / cache.file_key(path))
    elif args.purge_older:
        removed = cache.purge_older(root, args.purge_older)
        print(f"removed {len(removed)} cache item(s) unused for {args.purge_older:g}+ days from {root}")
    else:
        rows = cache.list_entries(root)
        for row in rows:
            used = datetime.fromtimestamp(row["last_used"]).strftime("%Y-%m-%d %H:%M")
            print(f"{row['item']}  {row['size_bytes'] / 1e6:9.1f} MB  {used}  {row['input']}")
        print(f"{len(rows)} item(s), {sum(r['size_bytes'] for r in rows) / 1e6:.1f} MB in {root}")
    return EXIT_OK


def cmd_frame(args):
    """L3 view: the frame on screen at each T, cropped to --roi, only ever downsized."""
    run = Run.open_from_out(args.out, args)
    roi = validated_roi(args.roi, run.probe)
    roi_label = "-".join(map(str, roi)) if roi else "full"
    for t in args.t:
        shown_t = run.snap_time(t)
        image = decode.read_frame(run.input_path, shown_t, width=args.width, roi=roi, stream_index=run.probe["video"]["index"])
        name = f"frames/f_{shown_t:.4f}_{roi_label}" + (f"_w{args.width}" if args.width else "") + ".png"
        view = views.write_view(run, views.fit_to_limits(image), name, level="L3", kind="frame", covers=f"{shown_t:.4f}")
        print(views.view_line(run, view))
    return EXIT_OK


def cmd_zoom(args):
    run = Run.open_from_out(args.out, args)
    if args.event:
        rendered = stage("sheets_motion", "zoom_event")(run, args.event, args.cells)
    elif args.segment:
        rendered = stage("sheets_content", "zoom_segment")(run, args.segment, args.cells)
    else:
        rendered = stage("sheets_content", "zoom_range")(run, args.range[0], args.range[1], args.cells)
    for view in rendered:
        role = "Read this" if view["level"] == "L2" else "optional native crop"
        print(f"{views.view_line(run, view)} · {role}")
    return EXIT_OK


def cmd_text(args):
    run = Run.open_from_out(args.out, args)
    sys.stdout.write(stage("report", "text_view")(run, args.start, args.end, args.grep, args.kind, args.max_lines))
    return EXIT_OK


def cmd_rows(args):
    run = Run.open_from_out(args.out, args)
    sys.stdout.write(stage("report", "rows_view")(run, args.kind, args.start, args.count))
    return EXIT_OK


def cmd_selftest(args):
    sys.path.insert(0, str(SELFTEST_DIR))
    return importlib.import_module("kat").run_selftest(quick=args.quick, only=args.only, chrome=args.chrome, keep=args.keep)


COMMANDS = {"analyze": cmd_analyze, "probe": cmd_probe, "cache": cmd_cache, "frame": cmd_frame, "zoom": cmd_zoom,
            "text": cmd_text, "rows": cmd_rows, "selftest": cmd_selftest}


def log_python_warning(message, category, filename, lineno, file=None, line=None):
    """numpy/cv2 warnings go to a log file: stderr must stay empty on success (O3)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "python-warnings.log", "a", encoding="utf-8") as log:
        log.write(f"{datetime.now().isoformat(timespec='seconds')} {category.__name__}: {message} ({filename}:{lineno})\n")


def main(argv=None):
    warnings.showwarning = log_python_warning
    try:
        args = parse_args(argv)
        return COMMANDS[args.command](args)
    except VlError as error:
        print(error.line(), file=sys.stderr)
        return error.code
    except KeyboardInterrupt:
        print("video-lens: interrupted. Rerun the same command; finished stages are cached", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return EXIT_OK
    except Exception as error:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        trace = LOG_DIR / "last-error.txt"
        trace.write_text(traceback.format_exc())
        print(f"video-lens: internal error ({type(error).__name__}: {error}). Traceback: {trace}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
