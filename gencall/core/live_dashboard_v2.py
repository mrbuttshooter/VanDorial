"""
GenCall - Terminal Live Dashboard (TUI)

Real-time terminal dashboard that runs WITHOUT a browser.
Pure ASCII art, ANSI colors, no external dependencies.
Shows live stats, active calls, CPS graph, and alerts right in the terminal.

Usage:
    gencall monitor          # Launch TUI dashboard
    gencall monitor --compact  # Compact mode
"""

import os
import sys
import time
import threading
import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger("gencall.tui")

# ANSI color codes
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"


def clear_screen():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def move_cursor(row: int, col: int):
    sys.stdout.write(f"\033[{row};{col}H")


def get_terminal_size() -> tuple[int, int]:
    try:
        cols, rows = os.get_terminal_size()
        return rows, cols
    except OSError:
        return 40, 120


def bar_chart(value: float, max_value: float, width: int = 30,
              fill_char: str = "█", empty_char: str = "░") -> str:
    """Generate a horizontal bar chart string."""
    if max_value <= 0:
        ratio = 0
    else:
        ratio = min(1.0, value / max_value)
    filled = int(width * ratio)
    empty = width - filled

    if ratio >= 0.8:
        color = C.GREEN
    elif ratio >= 0.5:
        color = C.YELLOW
    else:
        color = C.RED

    return f"{color}{fill_char * filled}{C.DIM}{empty_char * empty}{C.RESET}"


def sparkline(values: list[float], width: int = 40) -> str:
    """Generate a sparkline chart using Unicode block characters."""
    if not values:
        return " " * width

    # Take last `width` values
    data = list(values[-width:])
    if len(data) < width:
        data = [0.0] * (width - len(data)) + data

    max_val = max(data) if max(data) > 0 else 1
    blocks = " ▁▂▃▄▅▆▇█"

    result = ""
    for v in data:
        idx = int((v / max_val) * 8)
        idx = max(0, min(8, idx))
        result += blocks[idx]

    return f"{C.CYAN}{result}{C.RESET}"


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}m"
    else:
        return f"{seconds / 3600:.1f}h"


def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def status_badge(state: str) -> str:
    colors = {
        "running": f"{C.BG_GREEN}{C.WHITE} RUNNING {C.RESET}",
        "stopped": f"{C.DIM} STOPPED {C.RESET}",
        "error": f"{C.BG_RED}{C.WHITE} ERROR {C.RESET}",
        "starting": f"{C.BG_BLUE}{C.WHITE} STARTING {C.RESET}",
        "idle": f"{C.BG_YELLOW}{C.WHITE} IDLE {C.RESET}",
    }
    return colors.get(state, f" {state.upper()} ")


class TerminalDashboard:
    """
    Real-time terminal dashboard for GenCall.
    Renders live stats using ANSI escape codes.
    """

    def __init__(self, engine=None, stats_engine=None, refresh_rate: float = 1.0):
        self.engine = engine
        self.stats_engine = stats_engine
        self.refresh_rate = refresh_rate
        self._running = False
        self._thread = None
        self._cps_history: deque[float] = deque(maxlen=60)
        self._success_history: deque[float] = deque(maxlen=60)
        self._log_lines: deque[str] = deque(maxlen=15)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._render_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        clear_screen()

    def add_log(self, message: str):
        ts = time.strftime("%H:%M:%S")
        self._log_lines.append(f"{C.DIM}{ts}{C.RESET} {message}")

    def _render_loop(self):
        clear_screen()
        while self._running:
            try:
                self._render()
            except Exception as e:
                logger.debug("TUI render error: %s", e)
            time.sleep(self.refresh_rate)

    def _render(self):
        rows, cols = get_terminal_size()
        move_cursor(1, 1)

        stats = {}
        instances = []

        if self.stats_engine:
            stats = self.stats_engine.get_current()
        if self.engine:
            instances = self.engine.list_instances()

        cps = stats.get("calls_per_second", 0)
        self._cps_history.append(cps)
        self._success_history.append(stats.get("success_rate", 0))

        # ── Header ─────────────────────────────────────────────
        print(f"{C.BOLD}{C.CYAN}{'═' * cols}{C.RESET}")
        title = "  ██ GENCALL v2.0 ██  Live Monitor"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        padding = cols - len(title) - len(ts) - 4
        print(f"{C.BOLD}{C.CYAN}{title}{' ' * max(1, padding)}{C.DIM}{ts}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}{'═' * cols}{C.RESET}")
        print()

        # ── Stats Cards ────────────────────────────────────────
        active = stats.get("active_instances", 0)
        total = stats.get("total_calls", 0)
        success = stats.get("successful_calls", 0)
        failed = stats.get("failed_calls", 0)
        current = stats.get("current_calls", 0)
        sr = stats.get("success_rate", 0)
        rt = stats.get("avg_response_time_ms", 0)

        sr_color = C.GREEN if sr >= 95 else C.YELLOW if sr >= 80 else C.RED

        card_w = (cols - 8) // 4
        cards = [
            (f"{C.MAGENTA}ACTIVE TESTS{C.RESET}", f"{C.BOLD}{active}{C.RESET}"),
            (f"{C.CYAN}CALLS/SEC{C.RESET}", f"{C.BOLD}{C.GREEN}{cps:.1f}{C.RESET}"),
            (f"{C.GREEN}SUCCESS RATE{C.RESET}", f"{C.BOLD}{sr_color}{sr:.1f}%{C.RESET}"),
            (f"{C.BLUE}TOTAL CALLS{C.RESET}", f"{C.BOLD}{format_number(total)}{C.RESET}"),
        ]

        line1 = "  "
        line2 = "  "
        for label, value in cards:
            line1 += f"  {label:<{card_w}}"
            line2 += f"  {value:<{card_w}}"
        print(line1)
        print(line2)
        print()

        # ── Sub-stats ──────────────────────────────────────────
        print(f"  {C.DIM}Successful: {C.GREEN}{format_number(success)}{C.RESET}"
              f"  {C.DIM}Failed: {C.RED}{format_number(failed)}{C.RESET}"
              f"  {C.DIM}Current: {C.YELLOW}{current}{C.RESET}"
              f"  {C.DIM}Avg RT: {C.CYAN}{rt:.0f}ms{C.RESET}")
        print()

        # ── CPS Sparkline ──────────────────────────────────────
        chart_width = min(60, cols - 20)
        print(f"  {C.DIM}CPS (60s):{C.RESET} {sparkline(list(self._cps_history), chart_width)}"
              f" {C.BOLD}{cps:.1f}{C.RESET}")
        print(f"  {C.DIM}Success%: {C.RESET} {sparkline(list(self._success_history), chart_width)}"
              f" {C.BOLD}{sr:.1f}%{C.RESET}")
        print()

        # ── Active Tests Table ─────────────────────────────────
        print(f"  {C.BOLD}{'─' * (cols - 4)}{C.RESET}")
        print(f"  {C.BOLD}{'ID':<20} {'STATUS':<12} {'RATE':>6} {'CURRENT':>8} "
              f"{'TOTAL':>8} {'OK':>8} {'FAIL':>6} {'SUCCESS%':>9}{C.RESET}")
        print(f"  {C.DIM}{'─' * (cols - 4)}{C.RESET}")

        if instances:
            for inst in instances[:10]:  # Max 10 rows
                s = inst.get("stats", {})
                state = inst.get("state", "idle")
                inst_sr = s.get("success_rate", 0)
                sr_c = C.GREEN if inst_sr >= 95 else C.YELLOW if inst_sr >= 80 else C.RED

                print(f"  {inst['id']:<20} {status_badge(state)} "
                      f"{inst.get('call_rate', 0):>6.1f} "
                      f"{s.get('current_calls', 0):>8} "
                      f"{s.get('total_calls', 0):>8} "
                      f"{C.GREEN}{s.get('successful_calls', 0):>8}{C.RESET} "
                      f"{C.RED}{s.get('failed_calls', 0):>6}{C.RESET} "
                      f"{sr_c}{inst_sr:>8.1f}%{C.RESET}")
        else:
            print(f"  {C.DIM}  No active tests. Start one with: gencall run <target>{C.RESET}")

        print(f"  {C.DIM}{'─' * (cols - 4)}{C.RESET}")
        print()

        # ── Log Area ───────────────────────────────────────────
        print(f"  {C.BOLD}Activity Log{C.RESET}")
        log_lines = list(self._log_lines)
        max_log = min(10, rows - 25)
        if log_lines:
            for line in log_lines[-max_log:]:
                print(f"  {line}")
        else:
            print(f"  {C.DIM}Waiting for events...{C.RESET}")

        # ── Footer ─────────────────────────────────────────────
        remaining = rows - 25 - max(len(log_lines), 1) - 2
        for _ in range(max(0, remaining)):
            print()

        print(f"\n  {C.DIM}Press Ctrl+C to exit  │  "
              f"Refresh: {self.refresh_rate}s  │  "
              f"Uptime: {format_duration(stats.get('uptime_seconds', 0))}{C.RESET}")

        sys.stdout.flush()


def run_tui(engine=None, stats_engine=None):
    """Launch the terminal dashboard."""
    dash = TerminalDashboard(engine, stats_engine, refresh_rate=1.0)

    try:
        dash.start()
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        dash.stop()
        print(f"\n{C.CYAN}GenCall monitor stopped.{C.RESET}")
