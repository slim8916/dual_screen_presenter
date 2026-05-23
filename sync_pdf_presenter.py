#!/usr/bin/env python3
"""Synchronized dual-screen PDF presenter using Evince."""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


EVINCE_COMMAND = "evince"
POINTER_SHIELD_HELPER_ARG = "--pointer-shield-helper"
POINTER_SHIELD_EMIT_EVENTS_ARG = "--emit-events"
EVINCE_WINDOW_CLASSES = ("evince", "Evince", "org.gnome.Evince")
GENERIC_EVINCE_TITLES = {"", "document viewer", "recent documents"}
EVINCE_MODES = ("presentation", "fullscreen")
WINDOWED_EVINCE_MODE = "windowed"
DIRECT_EVINCE_MODE_SUFFIX = "-direct"
EVINCE_LAUNCH_BASE_MODES = (*EVINCE_MODES, WINDOWED_EVINCE_MODE)
EVINCE_LAUNCH_MODES = EVINCE_LAUNCH_BASE_MODES + tuple(
    f"{mode}{DIRECT_EVINCE_MODE_SUFFIX}" for mode in EVINCE_LAUNCH_BASE_MODES
)
PRESENTATION_LAUNCH_SEQUENCE = (
    f"{WINDOWED_EVINCE_MODE}{DIRECT_EVINCE_MODE_SUFFIX}",
    "presentation-direct",
    WINDOWED_EVINCE_MODE,
    "presentation",
    "fullscreen-direct",
    "fullscreen",
)
FULLSCREEN_LAUNCH_SEQUENCE = (
    f"{WINDOWED_EVINCE_MODE}{DIRECT_EVINCE_MODE_SUFFIX}",
    "fullscreen-direct",
    WINDOWED_EVINCE_MODE,
    "fullscreen",
)
WINDOWED_LAUNCH_SEQUENCE = (
    WINDOWED_EVINCE_MODE,
    f"{WINDOWED_EVINCE_MODE}{DIRECT_EVINCE_MODE_SUFFIX}",
)
REQUIRED_EVINCE_OPTIONS = ("--new-window", "--presentation", "--fullscreen")
WINDOW_TIMEOUT_SECONDS = 20.0
EXISTING_WINDOW_GRACE_SECONDS = 2.0
GENERIC_WINDOW_FAILURE_SECONDS = 3.0
DEBUG_WINDOW_SNAPSHOT_SECONDS = 2.0
KEY_SEQUENCE_TIMEOUT_SECONDS = 0.12


class PresenterError(Exception):
    """User-facing error."""


class GenericEvinceWindowError(PresenterError):
    """Raised when Evince opens its start window instead of the requested PDF."""

    def __init__(self, pdf_path: Path, windows: list["WindowInfo"]) -> None:
        self.windows = windows
        super().__init__(generic_evince_window_message(pdf_path, windows))


@dataclass(frozen=True)
class Monitor:
    name: str
    width: int
    height: int
    x: int
    y: int
    primary: bool = False


@dataclass(frozen=True)
class Rectangle:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class WindowInfo:
    window_id: int
    pid: int | None
    title: str
    window_class: str | None = None

    @property
    def wmctrl_id(self) -> str:
        return f"0x{self.window_id:08x}"

    @property
    def xdotool_id(self) -> str:
        return str(self.window_id)


@dataclass
class PresenterWindows:
    notes: WindowInfo
    slides: WindowInfo
    controller_window_id: int | None = None
    pointer_shield_process: subprocess.Popen[str] | None = None


@dataclass(frozen=True)
class PresentationPlan:
    notes_pdf: Path
    slides_pdf: Path
    notes_monitor: Monitor
    slides_monitor: Monitor
    start_page: int
    evince_mode: str
    swapped: bool = False


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open notes and slides PDFs on separate monitors and keep them synchronized.",
    )
    parser.add_argument("--notes", type=Path, help="speaker notes PDF")
    parser.add_argument("--notes-screen", help="monitor name for the notes PDF")
    parser.add_argument("--slides", type=Path, help="audience slides PDF")
    parser.add_argument("--slides-screen", help="monitor name for the slides PDF")
    parser.add_argument("--start-page", type=positive_int, default=1, help="initial page, default: 1")
    parser.add_argument(
        "--window-timeout",
        type=positive_float,
        default=WINDOW_TIMEOUT_SECONDS,
        help=f"seconds to wait for each Evince window, default: {WINDOW_TIMEOUT_SECONDS:g}",
    )
    parser.add_argument(
        "--swap-screens",
        action="store_true",
        help="swap the notes and slides monitor assignments",
    )
    parser.add_argument("--debug", action="store_true", help="print diagnostic information")
    parser.add_argument(
        "--evince-mode",
        choices=EVINCE_MODES,
        default="presentation",
        help=(
            "Evince display mode to try first, default: presentation. "
            "If presentation mode opens a generic Evince window, the script retries fullscreen."
        ),
    )
    parser.add_argument(
        "--close-on-exit",
        action="store_true",
        help="close the notes and slides Evince windows when the controller exits",
    )
    parser.add_argument(
        "--sync-pointer",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate inputs and print the planned actions without opening Evince",
    )
    parser.add_argument(
        "--force-wayland",
        action="store_true",
        help="continue under Wayland even though xdotool may not work reliably",
    )
    parser.add_argument(
        "--list-monitors",
        action="store_true",
        help="print active monitors detected by xrandr and exit",
    )

    args = parser.parse_args(argv)
    if not args.list_monitors:
        missing = [
            name
            for name, value in (
                ("--notes", args.notes),
                ("--notes-screen", args.notes_screen),
                ("--slides", args.slides),
                ("--slides-screen", args.slides_screen),
            )
            if value is None
        ]
        if missing:
            parser.error(f"missing required arguments unless --list-monitors is used: {', '.join(missing)}")
    return args


def debug_enabled(args: argparse.Namespace | bool) -> bool:
    return bool(args if isinstance(args, bool) else args.debug)


def debug_log(args: argparse.Namespace | bool, message: str) -> None:
    if debug_enabled(args):
        print(f"[debug] {message}", file=sys.stderr)


def run_command(
    command: list[str],
    *,
    debug: argparse.Namespace | bool = False,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    debug_log(debug, "$ " + format_command(command))
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PresenterError(f"Required command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PresenterError(f"Command timed out: {' '.join(command)}") from exc

    if check and result.returncode != 0:
        raise PresenterError(f"Command failed: {format_command(command)}\n{command_error(result)}")
    return result


def format_command(command: list[str]) -> str:
    return shlex.join(command)


def command_error(result: subprocess.CompletedProcess[str]) -> str:
    stderr = result.stderr.strip()
    stdout = result.stdout.strip()
    return stderr or stdout or f"exit code {result.returncode}"


def check_dependencies(commands: Iterable[str]) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        install = "sudo apt install evince xdotool wmctrl x11-xserver-utils"
        raise PresenterError(
            "Missing required command(s): "
            + ", ".join(missing)
            + "\nOn Debian/Ubuntu, install them with:\n    "
            + install
        )


def required_dependencies(args: argparse.Namespace) -> list[str]:
    if args.list_monitors:
        return ["xrandr"]
    if args.dry_run:
        return [EVINCE_COMMAND, "xrandr"]
    return [EVINCE_COMMAND, "xdotool", "wmctrl", "xrandr"]


def missing_required_evince_options(help_text: str) -> list[str]:
    return [option for option in REQUIRED_EVINCE_OPTIONS if option not in help_text]


def check_evince_features(debug: argparse.Namespace | bool = False) -> None:
    result = run_command([EVINCE_COMMAND, "--help"], debug=debug, check=False)
    if result.returncode != 0:
        raise PresenterError(f"Could not inspect Evince options:\n{command_error(result)}")

    missing = missing_required_evince_options(result.stdout + result.stderr)
    if missing:
        raise PresenterError(
            "Installed Evince does not advertise required option(s): "
            + ", ".join(missing)
            + "\nPlease install a newer Evince version."
        )


def check_session(force_wayland: bool) -> None:
    session_type = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session_type == "wayland" and not force_wayland:
        raise PresenterError(
            "This session is Wayland. xdotool and wmctrl are X11 tools and may not work reliably.\n"
            "Use an Xorg session for best results, or rerun with --force-wayland if you want to try anyway."
        )
    if session_type == "wayland" and force_wayland:
        print(
            "Warning: running under Wayland. xdotool and wmctrl may not control windows reliably.",
            file=sys.stderr,
        )


def check_x_display() -> None:
    if not os.environ.get("DISPLAY"):
        raise PresenterError(
            "No X display is available because $DISPLAY is not set.\n"
            "Run this from a graphical X11/Xorg session. Under Wayland, X11 tools may still fail."
        )


def validate_pdf(path: Path, label: str) -> Path:
    expanded = path.expanduser().resolve()
    if not expanded.exists():
        raise PresenterError(f"{label} PDF does not exist: {expanded}")
    if not expanded.is_file():
        raise PresenterError(f"{label} PDF is not a file: {expanded}")
    if expanded.suffix.lower() != ".pdf":
        raise PresenterError(f"{label} file must have a .pdf extension: {expanded}")
    return expanded


def parse_xrandr_monitors(output: str) -> dict[str, Monitor]:
    monitors: dict[str, Monitor] = {}
    pattern = re.compile(
        r"^(?P<name>\S+)\s+connected"
        r"(?P<primary>\s+primary)?\s+"
        r"(?P<width>\d+)x(?P<height>\d+)"
        r"(?P<x>[+-]\d+)(?P<y>[+-]\d+)"
    )
    for line in output.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        monitor = Monitor(
            name=match.group("name"),
            width=int(match.group("width")),
            height=int(match.group("height")),
            x=int(match.group("x")),
            y=int(match.group("y")),
            primary=bool(match.group("primary")),
        )
        monitors[monitor.name] = monitor
    return monitors


def detect_monitors(debug: argparse.Namespace | bool = False) -> dict[str, Monitor]:
    check_x_display()
    try:
        result = run_command(["xrandr", "--query"], debug=debug)
    except PresenterError as exc:
        raise PresenterError(
            "Could not query monitors with xrandr.\n"
            "Make sure you are running this from a graphical X11/Xorg session and that $DISPLAY is valid.\n"
            f"{exc}"
        ) from exc
    monitors = parse_xrandr_monitors(result.stdout)
    debug_log(debug, f"detected monitors: {monitors}")
    if not monitors:
        raise PresenterError("No active monitors were detected by xrandr.")
    return monitors


def format_monitor(monitor: Monitor) -> str:
    primary = " primary" if monitor.primary else ""
    return (
        f"{monitor.name}: {monitor.width}x{monitor.height}"
        f"{format_offset(monitor.x)}{format_offset(monitor.y)}{primary}"
    )


def format_offset(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)


def monitor_rectangle(monitor: Monitor) -> Rectangle:
    return Rectangle(x=monitor.x, y=monitor.y, width=monitor.width, height=monitor.height)


def format_rectangle(rectangle: Rectangle) -> str:
    return f"{rectangle.x},{rectangle.y},{rectangle.width},{rectangle.height}"


def parse_rectangle(value: str) -> Rectangle:
    parts = value.split(",")
    if len(parts) != 4:
        raise ValueError("rectangle must be x,y,width,height")
    x, y, width, height = (int(part) for part in parts)
    if width <= 0 or height <= 0:
        raise ValueError("rectangle width and height must be positive")
    return Rectangle(x=x, y=y, width=width, height=height)


def print_monitors(monitors: dict[str, Monitor]) -> None:
    print("Active monitors:")
    for monitor in monitors.values():
        print(f"  {format_monitor(monitor)}")


def require_monitor(monitors: dict[str, Monitor], name: str, label: str) -> Monitor:
    try:
        return monitors[name]
    except KeyError as exc:
        available = "\n".join(f"  {format_monitor(monitor)}" for monitor in monitors.values())
        raise PresenterError(
            f"Invalid {label} monitor: {name}\nAvailable active monitors:\n{available}"
        ) from exc


def list_windows(debug: argparse.Namespace | bool = False) -> list[WindowInfo]:
    result = run_command(["wmctrl", "-lxp"], debug=debug, check=False)
    if result.returncode != 0:
        debug_log(debug, f"wmctrl -lxp failed: {result.stderr.strip()}")
        result = run_command(["wmctrl", "-lp"], debug=debug, check=False)
        if result.returncode != 0:
            debug_log(debug, f"wmctrl -lp failed: {result.stderr.strip()}")
            return []
        return parse_wmctrl_windows(result.stdout, includes_class=False)

    return parse_wmctrl_windows(result.stdout, includes_class=True)


def parse_wmctrl_windows(output: str, *, includes_class: bool) -> list[WindowInfo]:
    """Parse wmctrl window listings.

    With -lxp, wmctrl prints: id desktop pid wm_class host title.
    With -lp, it prints: id desktop pid host title.
    """
    title_index = 5 if includes_class else 4
    min_parts = 5 if includes_class else 4

    windows: list[WindowInfo] = []
    for line in output.splitlines():
        parts = line.split(None, title_index)
        if len(parts) < min_parts:
            continue
        try:
            window_id = int(parts[0], 16)
        except ValueError:
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            pid = None
        if pid == 0:
            pid = None
        window_class = parts[3] if includes_class and len(parts) >= 4 else None
        title = parts[title_index] if len(parts) > title_index else ""
        windows.append(WindowInfo(window_id=window_id, pid=pid, title=title, window_class=window_class))
    return windows


def search_evince_window_ids(debug: argparse.Namespace | bool = False) -> set[int]:
    ids: set[int] = set()
    for window_class in EVINCE_WINDOW_CLASSES:
        result = run_command(
            ["xdotool", "search", "--onlyvisible", "--class", window_class],
            debug=debug,
            check=False,
        )
        if result.returncode != 0:
            debug_log(debug, f"xdotool class search failed for {window_class!r}: {result.stderr.strip()}")
            continue

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(int(line, 0))
            except ValueError:
                debug_log(debug, f"ignoring unexpected xdotool window id: {line}")
    return ids


def title_matches_pdf(title: str, pdf_path: Path) -> bool:
    lowered_title = title.casefold()
    pdf_name = pdf_path.name.casefold()
    pdf_stem = pdf_path.stem.casefold()
    return pdf_name in lowered_title or (len(pdf_stem) >= 3 and pdf_stem in lowered_title)


def is_generic_evince_title(title: str) -> bool:
    return title.strip().casefold() in GENERIC_EVINCE_TITLES


def window_class_looks_like_evince(window: WindowInfo) -> bool:
    if not window.window_class:
        return False
    lowered = window.window_class.casefold()
    return "evince" in lowered


def describe_window(window: WindowInfo) -> str:
    window_class = window.window_class or "?"
    return f"{window.wmctrl_id} pid={window.pid} class={window_class!r} title={window.title!r}"


def format_window_snapshot(
    windows: list[WindowInfo],
    *,
    before_ids: set[int],
    evince_ids: set[int],
    process_pid: int,
) -> str:
    if not windows:
        return "  none"

    lines: list[str] = []
    for window in windows:
        flags: list[str] = []
        flags.append("new" if window.window_id not in before_ids else "old")
        if window.pid == process_pid:
            flags.append("pid-match")
        if window.window_id in evince_ids:
            flags.append("xdotool-evince")
        if window_class_looks_like_evince(window):
            flags.append("wmclass-evince")
        if is_generic_evince_title(window.title):
            flags.append("generic-title")
        lines.append(f"  {describe_window(window)} [{' '.join(flags)}]")
    return "\n".join(lines)


def generic_evince_window_message(pdf_path: Path, windows: list[WindowInfo]) -> str:
    matching_windows = "\n".join(f"  {describe_window(window)}" for window in windows)
    return (
        f"Evince opened a generic start window instead of {pdf_path.name}.\n"
        "This usually means Evince did not accept or load the PDF from the command line.\n"
        "Generic Evince window(s):\n"
        + (matching_windows or "  none")
    )


def wait_for_evince_window(
    *,
    pdf_path: Path,
    before_ids: set[int],
    process: subprocess.Popen[str],
    debug: argparse.Namespace | bool = False,
    timeout: float = WINDOW_TIMEOUT_SECONDS,
) -> WindowInfo:
    deadline = time.monotonic() + timeout
    allow_existing_at = time.monotonic() + EXISTING_WINDOW_GRACE_SECONDS
    generic_seen_at: float | None = None
    latest_generic_windows: list[WindowInfo] = []
    last_debug_snapshot_at = 0.0
    process_pid = process.pid

    while time.monotonic() < deadline:
        now = time.monotonic()
        allow_existing = time.monotonic() >= allow_existing_at or process.poll() is not None
        evince_ids = search_evince_window_ids(False)
        windows = list_windows(False)
        scored: list[tuple[int, WindowInfo]] = []
        generic_windows: list[WindowInfo] = []

        for window in windows:
            is_new = window.window_id not in before_ids
            is_evince = window.window_id in evince_ids or window_class_looks_like_evince(window)
            pid_match = window.pid == process_pid
            title_match = title_matches_pdf(window.title, pdf_path)
            generic_title = is_generic_evince_title(window.title)

            if not is_evince:
                continue

            if generic_title and (is_new or pid_match) and is_evince:
                generic_windows.append(window)

            if not (
                (is_new or pid_match)
                and not generic_title
                and (title_match or allow_existing)
            ):
                continue

            score = 0
            if pid_match:
                score += 8
            if title_match:
                score += 6
            if is_new:
                score += 4
            else:
                score -= 4
            if is_evince:
                score += 3
            scored.append((score, window))

        if debug_enabled(debug) and now - last_debug_snapshot_at >= DEBUG_WINDOW_SNAPSHOT_SECONDS:
            debug_log(
                debug,
                f"windows while waiting for {pdf_path.name}:\n"
                + format_window_snapshot(
                    windows,
                    before_ids=before_ids,
                    evince_ids=evince_ids,
                    process_pid=process_pid,
                ),
            )
            last_debug_snapshot_at = now

        if scored:
            scored.sort(key=lambda item: item[0], reverse=True)
            window = scored[0][1]
            debug_log(
                debug,
                f"matched {pdf_path.name} to window {window.wmctrl_id} "
                f"pid={window.pid} class={window.window_class!r} title={window.title!r}",
            )
            return window

        if generic_windows:
            latest_generic_windows = generic_windows
            if generic_seen_at is None:
                generic_seen_at = now
            elif now - generic_seen_at >= GENERIC_WINDOW_FAILURE_SECONDS:
                raise GenericEvinceWindowError(pdf_path, latest_generic_windows)
        else:
            generic_seen_at = None
            latest_generic_windows = []

        if process.poll() is not None:
            debug_log(debug, f"evince process {process_pid} exited with code {process.returncode}")
            diagnostics = collect_process_diagnostics(process)
            if diagnostics:
                debug_log(debug, f"evince output for {pdf_path.name}: {diagnostics}")

        time.sleep(0.25)

    windows = list_windows(debug)
    titles = "\n".join(f"  {describe_window(window)}" for window in windows)
    generic_hint = ""
    if latest_generic_windows:
        generic_hint = generic_evince_window_message(pdf_path, latest_generic_windows) + "\n"
    raise PresenterError(
        f"Timed out waiting for Evince window for {pdf_path.name}.\n"
        + generic_hint
        + process_diagnostics_for_error(process)
        + "Known windows:\n"
        + (titles or "  none")
    )


def evince_file_argument(pdf_path: Path, _start_page: int) -> str:
    resolved = pdf_path.expanduser().resolve()
    return str(resolved)


def evince_base_mode(mode: str) -> str:
    return mode.removesuffix(DIRECT_EVINCE_MODE_SUFFIX)


def evince_uses_new_window(mode: str) -> bool:
    return not mode.endswith(DIRECT_EVINCE_MODE_SUFFIX)


def evince_mode_label(mode: str) -> str:
    base_mode = evince_base_mode(mode)
    if evince_uses_new_window(mode):
        return base_mode
    if base_mode == WINDOWED_EVINCE_MODE:
        return "windowed without --new-window"
    return f"{base_mode} without --new-window"


def build_evince_command(pdf_path: Path, start_page: int, mode: str = "presentation") -> list[str]:
    if mode not in EVINCE_LAUNCH_MODES:
        raise ValueError(f"unsupported Evince mode: {mode}")

    base_mode = evince_base_mode(mode)
    command = [EVINCE_COMMAND]
    if evince_uses_new_window(mode):
        command.append("--new-window")
    if base_mode == "presentation":
        command.append("--presentation")
    elif base_mode == "fullscreen":
        command.append("--fullscreen")
    command.append(evince_file_argument(pdf_path, start_page))
    return command


def evince_mode_sequence(preferred_mode: str) -> list[str]:
    base_mode = evince_base_mode(preferred_mode)
    if base_mode == "presentation":
        sequence = PRESENTATION_LAUNCH_SEQUENCE
    elif base_mode == "fullscreen":
        sequence = FULLSCREEN_LAUNCH_SEQUENCE
    else:
        sequence = WINDOWED_LAUNCH_SEQUENCE

    if preferred_mode == base_mode:
        return list(sequence)

    try:
        start_index = sequence.index(preferred_mode)
    except ValueError:
        return [preferred_mode]
    return list(sequence[start_index:])


def collect_process_diagnostics(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        return ""
    try:
        stdout, stderr = process.communicate(timeout=0.2)
    except (ValueError, subprocess.TimeoutExpired):
        return ""
    return "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())


def process_diagnostics_for_error(process: subprocess.Popen[str]) -> str:
    diagnostics = collect_process_diagnostics(process)
    if not diagnostics:
        return ""
    return f"Evince output:\n{diagnostics}\n"


def build_plan(args: argparse.Namespace) -> PresentationPlan:
    notes_pdf = validate_pdf(args.notes, "Notes")
    slides_pdf = validate_pdf(args.slides, "Slides")
    if notes_pdf == slides_pdf:
        raise PresenterError("Notes and slides PDFs must be different files.")

    monitors = detect_monitors(args)
    notes_monitor = require_monitor(monitors, args.notes_screen, "notes")
    slides_monitor = require_monitor(monitors, args.slides_screen, "slides")

    if notes_monitor.name == slides_monitor.name:
        raise PresenterError("Notes and slides monitors must be different for dual-screen mode.")

    if args.swap_screens:
        notes_monitor, slides_monitor = slides_monitor, notes_monitor

    return PresentationPlan(
        notes_pdf=notes_pdf,
        slides_pdf=slides_pdf,
        notes_monitor=notes_monitor,
        slides_monitor=slides_monitor,
        start_page=args.start_page,
        evince_mode=args.evince_mode,
        swapped=args.swap_screens,
    )


def print_dry_run(plan: PresentationPlan) -> None:
    print("Dry run: no windows will be opened.")
    print(f"Notes PDF:  {plan.notes_pdf}")
    print(f"Slides PDF: {plan.slides_pdf}")
    print(f"Notes monitor:  {format_monitor(plan.notes_monitor)}")
    print(f"Slides monitor: {format_monitor(plan.slides_monitor)}")
    if plan.swapped:
        print("Screen assignments were swapped by --swap-screens.")
    print(f"Start page: {plan.start_page}")
    print(f"Evince mode: {plan.evince_mode}")
    print("Evince launch attempts:")
    for index, mode in enumerate(evince_mode_sequence(plan.evince_mode)):
        suffix = " fallback" if index > 0 else ""
        print(f"  {evince_mode_label(mode)}{suffix}:")
        print(f"    {format_command(build_evince_command(plan.notes_pdf, plan.start_page, mode))}")
        print(f"    {format_command(build_evince_command(plan.slides_pdf, plan.start_page, mode))}")
    if plan.evince_mode == "presentation":
        print("After positioning: send F5 to each Evince window to enter slideshow presentation mode.")
    if plan.start_page > 1:
        print(
            f"After presentation setup: send Page_Down {plan.start_page - 1} time(s) "
            "to both PDFs to reach the start page."
        )


def launch_evince(
    pdf_path: Path,
    start_page: int,
    mode: str,
    debug: argparse.Namespace | bool = False,
) -> subprocess.Popen[str]:
    command = build_evince_command(pdf_path, start_page, mode)
    debug_log(debug, "$ " + format_command(command))
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise PresenterError("Evince is not installed or not available on PATH.") from exc


def open_evince_pdf_window(
    *,
    pdf_path: Path,
    start_page: int,
    preferred_mode: str,
    before_ids: set[int],
    launched_processes: list[subprocess.Popen[str]],
    debug: argparse.Namespace | bool = False,
    timeout: float = WINDOW_TIMEOUT_SECONDS,
) -> tuple[WindowInfo, str]:
    errors: list[str] = []
    modes = evince_mode_sequence(preferred_mode)
    for index, mode in enumerate(modes):
        process = launch_evince(pdf_path, start_page, mode, debug)
        launched_processes.append(process)
        try:
            window = wait_for_evince_window(
                pdf_path=pdf_path,
                before_ids=before_ids,
                process=process,
                debug=debug,
                timeout=timeout,
            )
            return window, mode
        except PresenterError as exc:
            if isinstance(exc, GenericEvinceWindowError):
                for generic_window in exc.windows:
                    close_window(generic_window, debug)
            cleanup_launched_processes([process], debug, process_label="Evince process")
            errors.append(f"{mode}: {exc}")
            if index + 1 < len(modes):
                next_mode = modes[index + 1]
                print(
                    f"Warning: Evince did not expose {pdf_path.name} using "
                    f"{evince_mode_label(mode)}; retrying with {evince_mode_label(next_mode)}.",
                    file=sys.stderr,
                )

    details = "\n\n".join(errors)
    raise PresenterError(f"Could not open an Evince window for {pdf_path.name}.\n{details}")


def move_window_to_monitor(
    window: WindowInfo,
    monitor: Monitor,
    *,
    make_fullscreen: bool = True,
    debug: argparse.Namespace | bool = False,
) -> None:
    debug_log(debug, f"moving {window.wmctrl_id} to {format_monitor(monitor)}")
    run_command(
        ["wmctrl", "-ir", window.wmctrl_id, "-b", "remove,fullscreen,maximized_vert,maximized_horz"],
        debug=debug,
        check=False,
    )
    time.sleep(0.2)
    wmctrl_move = run_command(
        [
            "wmctrl",
            "-ir",
            window.wmctrl_id,
            "-e",
            f"0,{monitor.x},{monitor.y},{monitor.width},{monitor.height}",
        ],
        debug=debug,
        check=False,
    )
    xdotool_move = run_command(
        ["xdotool", "windowmove", window.xdotool_id, str(monitor.x), str(monitor.y)],
        debug=debug,
        check=False,
    )
    run_command(
        ["xdotool", "windowsize", window.xdotool_id, str(monitor.width), str(monitor.height)],
        debug=debug,
        check=False,
    )
    time.sleep(0.2)
    if wmctrl_move.returncode != 0 and xdotool_move.returncode != 0:
        raise PresenterError(
            f"Could not move window {window.wmctrl_id} to {monitor.name}.\n"
            f"wmctrl: {command_error(wmctrl_move)}\n"
            f"xdotool: {command_error(xdotool_move)}"
        )

    if make_fullscreen:
        fullscreen = run_command(
            ["wmctrl", "-ir", window.wmctrl_id, "-b", "add,fullscreen"],
            debug=debug,
            check=False,
        )
        if fullscreen.returncode != 0:
            print(
                f"Warning: could not set window {window.wmctrl_id} fullscreen: {command_error(fullscreen)}",
                file=sys.stderr,
            )


def activate_window(window: WindowInfo, debug: argparse.Namespace | bool = False) -> None:
    activate_window_id(window.window_id, debug)


def activate_window_id(window_id: int, debug: argparse.Namespace | bool = False) -> None:
    window_arg = str(window_id)
    result = run_command(
        ["xdotool", "windowactivate", "--sync", window_arg],
        debug=debug,
        check=False,
    )
    if result.returncode != 0:
        raise PresenterError(f"Could not activate window {window_arg}: {command_error(result)}")
    time.sleep(0.1)


def focus_window_id(window_id: int, debug: argparse.Namespace | bool = False) -> None:
    window_arg = str(window_id)
    result = run_command(
        ["xdotool", "windowfocus", "--sync", window_arg],
        debug=debug,
        check=False,
    )
    if result.returncode != 0:
        raise PresenterError(f"Could not focus window {window_arg}: {command_error(result)}")
    time.sleep(0.1)


def get_active_window_id(debug: argparse.Namespace | bool = False) -> int | None:
    result = run_command(["xdotool", "getactivewindow"], debug=debug, check=False)
    if result.returncode != 0:
        debug_log(debug, f"could not get active window: {command_error(result)}")
        return None
    try:
        return int(result.stdout.strip(), 0)
    except ValueError:
        debug_log(debug, f"unexpected active window id: {result.stdout.strip()!r}")
        return None


def restore_controller_focus(
    windows: PresenterWindows,
    debug: argparse.Namespace | bool = False,
) -> None:
    if windows.controller_window_id is None:
        return
    try:
        focus_window_id(windows.controller_window_id, debug)
    except PresenterError as exc:
        print(f"Warning: could not restore keyboard focus to the controller terminal: {exc}", file=sys.stderr)


def enter_evince_presentation_mode(window: WindowInfo, debug: argparse.Namespace | bool = False) -> None:
    debug_log(debug, f"entering Evince presentation mode for {window.wmctrl_id}")
    send_key(window, "F5", debug)


def close_window(window: WindowInfo, debug: argparse.Namespace | bool = False) -> None:
    wmctrl_close = run_command(
        ["wmctrl", "-ic", window.wmctrl_id],
        debug=debug,
        check=False,
    )
    if wmctrl_close.returncode == 0:
        return

    xdotool_close = run_command(
        ["xdotool", "windowclose", window.xdotool_id],
        debug=debug,
        check=False,
    )
    if xdotool_close.returncode != 0:
        print(
            f"Warning: could not close window {window.wmctrl_id}: "
            f"wmctrl: {command_error(wmctrl_close)}; "
            f"xdotool: {command_error(xdotool_close)}",
            file=sys.stderr,
        )


def close_presenter_windows(windows: PresenterWindows, debug: argparse.Namespace | bool = False) -> None:
    close_window(windows.notes, debug)
    close_window(windows.slides, debug)


class XSetWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("background_pixmap", ctypes.c_ulong),
        ("background_pixel", ctypes.c_ulong),
        ("border_pixmap", ctypes.c_ulong),
        ("border_pixel", ctypes.c_ulong),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("cursor", ctypes.c_ulong),
    ]


class XButtonEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("button", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


def configure_x11(lib_x11: ctypes.CDLL) -> None:
    lib_x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib_x11.XOpenDisplay.restype = ctypes.c_void_p
    lib_x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
    lib_x11.XDefaultScreen.restype = ctypes.c_int
    lib_x11.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib_x11.XRootWindow.restype = ctypes.c_ulong
    lib_x11.XCreateWindow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(XSetWindowAttributes),
    ]
    lib_x11.XCreateWindow.restype = ctypes.c_ulong
    lib_x11.XMapRaised.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib_x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib_x11.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib_x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib_x11.XFlush.argtypes = [ctypes.c_void_p]
    lib_x11.XPending.argtypes = [ctypes.c_void_p]
    lib_x11.XPending.restype = ctypes.c_int
    lib_x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


def pointer_action_for_button(button: int) -> str | None:
    if button in (1, 5):
        return "next"
    if button in (3, 4):
        return "previous"
    return None


def run_pointer_shield(rectangles: list[Rectangle], *, emit_events: bool = False) -> int:
    if not rectangles:
        print("pointer shield needs at least one rectangle", file=sys.stderr)
        return 2

    try:
        lib_x11 = ctypes.CDLL("libX11.so.6")
    except OSError as exc:
        print(f"could not load libX11: {exc}", file=sys.stderr)
        return 1

    configure_x11(lib_x11)
    display = lib_x11.XOpenDisplay(None)
    if not display:
        print("could not open X display for pointer shield", file=sys.stderr)
        return 1

    input_only = 2
    cw_override_redirect = 1 << 9
    cw_event_mask = 1 << 11
    cw_do_not_propagate = 1 << 12
    button_press_mask = 1 << 2
    button_release_mask = 1 << 3
    pointer_motion_mask = 1 << 6
    event_mask = button_press_mask | button_release_mask | pointer_motion_mask
    value_mask = cw_override_redirect | cw_event_mask | cw_do_not_propagate
    windows: list[int] = []
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    old_sigterm = signal.signal(signal.SIGTERM, stop)
    old_sigint = signal.signal(signal.SIGINT, stop)
    try:
        screen = lib_x11.XDefaultScreen(display)
        root = lib_x11.XRootWindow(display, screen)

        for rectangle in rectangles:
            attrs = XSetWindowAttributes()
            attrs.override_redirect = 1
            attrs.event_mask = event_mask
            attrs.do_not_propagate_mask = event_mask
            window = lib_x11.XCreateWindow(
                display,
                root,
                rectangle.x,
                rectangle.y,
                rectangle.width,
                rectangle.height,
                0,
                0,
                input_only,
                None,
                value_mask,
                ctypes.byref(attrs),
            )
            if not window:
                continue
            windows.append(window)
            lib_x11.XMapRaised(display, window)
            lib_x11.XRaiseWindow(display, window)

        lib_x11.XFlush(display)
        event_buffer = ctypes.create_string_buffer(192)
        while running:
            for window in windows:
                lib_x11.XRaiseWindow(display, window)
            lib_x11.XFlush(display)
            while lib_x11.XPending(display):
                lib_x11.XNextEvent(display, ctypes.byref(event_buffer))
                if not emit_events:
                    continue
                event_type = ctypes.c_int.from_buffer_copy(event_buffer).value
                if event_type != 4:  # ButtonPress
                    continue
                button_event = XButtonEvent.from_buffer_copy(event_buffer)
                action = pointer_action_for_button(button_event.button)
                if action:
                    print(action, flush=True)
            time.sleep(0.2)
    finally:
        for window in windows:
            lib_x11.XDestroyWindow(display, window)
        lib_x11.XFlush(display)
        lib_x11.XCloseDisplay(display)
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)

    return 0


def run_pointer_shield_helper(argv: list[str]) -> int:
    try:
        libc = ctypes.CDLL(None)
        libc.prctl(1, signal.SIGTERM)
    except (AttributeError, OSError):
        pass

    emit_events = False
    rectangle_args = argv
    if rectangle_args and rectangle_args[0] == POINTER_SHIELD_EMIT_EVENTS_ARG:
        emit_events = True
        rectangle_args = rectangle_args[1:]

    try:
        rectangles = [parse_rectangle(value) for value in rectangle_args]
    except ValueError as exc:
        print(f"invalid pointer shield rectangle: {exc}", file=sys.stderr)
        return 2
    return run_pointer_shield(rectangles, emit_events=emit_events)


def build_pointer_shield_command(rectangles: list[Rectangle], *, emit_events: bool = False) -> list[str]:
    event_args = [POINTER_SHIELD_EMIT_EVENTS_ARG] if emit_events else []
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        POINTER_SHIELD_HELPER_ARG,
        *event_args,
        *(format_rectangle(rectangle) for rectangle in rectangles),
    ]


def start_pointer_shield(
    rectangles: list[Rectangle],
    debug: argparse.Namespace | bool = False,
    *,
    emit_events: bool = False,
) -> subprocess.Popen[str] | None:
    command = build_pointer_shield_command(rectangles, emit_events=emit_events)
    debug_log(debug, "$ " + format_command(command))
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if emit_events else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        print(f"Warning: could not start pointer shield: {exc}", file=sys.stderr)
        return None

    time.sleep(0.2)
    if process.poll() is not None:
        diagnostics = collect_process_diagnostics(process)
        print(
            "Warning: pointer controller exited before it could handle mouse/touch input"
            + (f": {diagnostics}" if diagnostics else "."),
            file=sys.stderr,
        )
        return None
    return process


def cleanup_pointer_shield(
    windows: PresenterWindows,
    debug: argparse.Namespace | bool = False,
) -> None:
    if windows.pointer_shield_process is None:
        return
    cleanup_launched_processes(
        [windows.pointer_shield_process],
        debug,
        process_label="pointer shield process",
    )
    windows.pointer_shield_process = None


def send_key(window: WindowInfo, key: str, debug: argparse.Namespace | bool = False) -> None:
    activate_window(window, debug)
    run_command(
        ["xdotool", "key", "--clearmodifiers", key],
        debug=debug,
        check=True,
    )


def send_to_both(windows: PresenterWindows, key: str, debug: argparse.Namespace | bool = False) -> None:
    failures: list[str] = []
    try:
        for label, window in (("notes", windows.notes), ("slides", windows.slides)):
            try:
                send_key(window, key, debug)
            except PresenterError as exc:
                failures.append(f"{label}: {exc}")
    finally:
        restore_controller_focus(windows, debug)

    if failures:
        details = "\n".join(f"  {failure}" for failure in failures)
        raise PresenterError(f"Could not send {key} to all presentation windows:\n{details}")


def advance_to_start_page(
    windows: PresenterWindows,
    start_page: int,
    debug: argparse.Namespace | bool = False,
) -> None:
    if start_page <= 1:
        return
    debug_log(debug, f"advancing both PDFs to start page {start_page}")
    for _ in range(start_page - 1):
        send_to_both(windows, "Page_Down", debug)


def read_terminal_key() -> str:
    fd = sys.stdin.fileno()
    key = os.read(fd, 1).decode("latin1", errors="ignore")
    if key != "\x1b":
        return key

    sequence = [key]
    while select.select([fd], [], [], KEY_SEQUENCE_TIMEOUT_SECONDS)[0]:
        char = os.read(fd, 1).decode("latin1", errors="ignore")
        sequence.append(char)
        if char == "~" or (len(sequence) >= 3 and char.isalpha()):
            break
        if len(sequence) >= 8:
            break
    return "".join(sequence)


def controller_action_for_key(key: str) -> str | None:
    if key.startswith(("\x1b[", "\x1bO")):
        if key.endswith(("B", "C")):
            return "next"
        if key.endswith(("A", "D")):
            return "previous"
        if key.startswith("\x1b[6") and key.endswith("~"):
            return "next"
        if key.startswith("\x1b[5") and key.endswith("~"):
            return "previous"

    next_keys = {
        "n",
        "N",
        " ",
        "\x1b[B",  # Down
        "\x1bOB",
        "\x1b[C",  # Right
        "\x1bOC",
        "\x1b[6~",  # PageDown
    }
    previous_keys = {
        "p",
        "P",
        "\x1b[A",  # Up
        "\x1bOA",
        "\x1b[D",  # Left
        "\x1bOD",
        "\x1b[5~",  # PageUp
        "\x7f",
        "\b",
    }
    help_keys = {"h", "H", "?"}
    quit_keys = {"q", "Q", "\x03"}

    if key in next_keys:
        return "next"
    if key in previous_keys:
        return "previous"
    if key in help_keys:
        return "help"
    if key in quit_keys:
        return "quit"
    return None


def print_controller_help() -> None:
    print(
        "\nControls:\n"
        "  next:     n, Right, Down, PageDown, Space\n"
        "  previous: p, Left, Up, PageUp, BackSpace\n"
        "  help:     h\n"
        "  quit:     q\n"
    )


def run_controller(windows: PresenterWindows, debug: argparse.Namespace | bool = False) -> None:
    if not sys.stdin.isatty():
        raise PresenterError("Keyboard controller requires an interactive terminal.")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    print_controller_help()
    print("Controller is running. Press q to quit.")

    try:
        tty.setcbreak(fd)
        while True:
            read_fds = [fd]
            pointer_output = None
            pointer_fd = None
            if windows.pointer_shield_process is not None and windows.pointer_shield_process.stdout is not None:
                pointer_output = windows.pointer_shield_process.stdout
                pointer_fd = pointer_output.fileno()
                read_fds.append(pointer_fd)

            readable, _, _ = select.select(read_fds, [], [])
            action = None
            if fd in readable:
                key = read_terminal_key()
                debug_log(debug, f"controller key received: {key!r}")
                action = controller_action_for_key(key)
            elif pointer_output is not None and pointer_fd in readable:
                line = pointer_output.readline()
                if not line:
                    windows.pointer_shield_process = None
                    continue
                action = line.strip()
                debug_log(debug, f"pointer action received: {action!r}")

            if action == "next":
                send_to_both(windows, "Page_Down", debug)
            elif action == "previous":
                send_to_both(windows, "Page_Up", debug)
            elif action == "help":
                print_controller_help()
            elif action == "quit":
                break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def open_presenter(args: argparse.Namespace) -> PresenterWindows:
    plan = build_plan(args)
    opened_windows: list[WindowInfo] = []
    launched_processes: list[subprocess.Popen[str]] = []
    controller_window_id = get_active_window_id(args)

    try:
        existing = {window.window_id for window in list_windows(args)}
        notes_window, mode_used = open_evince_pdf_window(
            pdf_path=plan.notes_pdf,
            start_page=plan.start_page,
            preferred_mode=plan.evince_mode,
            before_ids=existing,
            launched_processes=launched_processes,
            debug=args,
            timeout=args.window_timeout,
        )
        opened_windows.append(notes_window)

        existing = {window.window_id for window in list_windows(args)}
        slides_window, mode_used = open_evince_pdf_window(
            pdf_path=plan.slides_pdf,
            start_page=plan.start_page,
            preferred_mode=mode_used,
            before_ids=existing,
            launched_processes=launched_processes,
            debug=args,
            timeout=args.window_timeout,
        )
        opened_windows.append(slides_window)

        use_evince_presentation = plan.evince_mode == "presentation"
        move_window_to_monitor(
            notes_window,
            plan.notes_monitor,
            make_fullscreen=not use_evince_presentation,
            debug=args,
        )
        move_window_to_monitor(
            slides_window,
            plan.slides_monitor,
            make_fullscreen=not use_evince_presentation,
            debug=args,
        )
        if use_evince_presentation:
            enter_evince_presentation_mode(notes_window, args)
            enter_evince_presentation_mode(slides_window, args)
        windows = PresenterWindows(
            notes=notes_window,
            slides=slides_window,
            controller_window_id=controller_window_id,
        )
        if use_evince_presentation:
            restore_controller_focus(windows, args)
        advance_to_start_page(windows, plan.start_page, args)
        windows.pointer_shield_process = start_pointer_shield(
            [
                monitor_rectangle(plan.notes_monitor),
                monitor_rectangle(plan.slides_monitor),
            ],
            args,
            emit_events=True,
        )
    except BaseException:
        cleanup_partial_setup(opened_windows, args)
        cleanup_launched_processes(launched_processes, args, process_label="Evince process")
        raise

    return windows


def cleanup_partial_setup(
    opened_windows: list[WindowInfo],
    debug: argparse.Namespace | bool = False,
) -> None:
    if not opened_windows:
        return

    print("Cleaning up partially opened Evince window(s)...", file=sys.stderr)
    for window in opened_windows:
        close_window(window, debug)


def cleanup_launched_processes(
    launched_processes: list[subprocess.Popen[str]],
    debug: argparse.Namespace | bool = False,
    process_label: str = "process",
) -> None:
    for process in launched_processes:
        try:
            if process.poll() is not None:
                continue

            debug_log(debug, f"terminating {process_label} {process.pid}")
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            debug_log(debug, f"killing {process_label} {process.pid}")
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                print(
                    f"Warning: {process_label} {process.pid} did not exit after kill().",
                    file=sys.stderr,
                )
        except OSError as exc:
            print(
                f"Warning: could not clean up {process_label} {process.pid}: {exc}",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if raw_argv and raw_argv[0] == POINTER_SHIELD_HELPER_ARG:
        return run_pointer_shield_helper(raw_argv[1:])

    args = parse_args(raw_argv)

    try:
        check_dependencies(required_dependencies(args))

        if args.list_monitors:
            print_monitors(detect_monitors(args))
            return 0

        check_session(args.force_wayland)
        check_evince_features(args)
        if args.dry_run:
            print_dry_run(build_plan(args))
            return 0

        windows = open_presenter(args)
        print("Presentation windows are ready.")
        try:
            run_controller(windows, args)
        finally:
            cleanup_pointer_shield(windows, args)
            if args.close_on_exit:
                close_presenter_windows(windows, args)

        if args.close_on_exit:
            print("\nController exited. Evince windows were closed.")
        else:
            print("\nController exited. Evince windows were left open.")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except PresenterError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    raise SystemExit(main())
