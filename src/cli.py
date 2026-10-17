#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import select
import shlex
import sys
import termios
import time
import tty

import pexpect

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

LIMIT_AT_RE = re.compile(
    r"you[’']ve hit your usage limit.*?try again\s+at\s+([^\r\n.]+)",
    re.IGNORECASE | re.DOTALL,
)

LIMIT_IN_RE = re.compile(
    r"you[’']ve hit your usage limit.*?try again\s+in\s+([^\r\n.]+)",
    re.IGNORECASE | re.DOTALL,
)

STATUS_RE = re.compile(
    r"limit:[ ]*\[[█░]*] ?([\d]*)% ?left ?\(resets? ?([0-9 :A-Za-z]*)\)",
    re.IGNORECASE
)

DURATION_RE = re.compile(
    r"(\d+)\s*"
    r"(weeks?|w|days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)",
    re.IGNORECASE,
)

ORDINAL_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)

def parse_absolute_time(s: str, now: dt.datetime) -> dt.datetime:
    s = strip_ansi(s).strip()
    s = s.rstrip(" .)")
    s = ORDINAL_RE.sub("", s)
    s = re.sub(r"\s+", " ", s)

    now = now.astimezone()

    formats = [
        "%I:%M %p",
        "%I %p",
        "%I:%M %p on %d %b",
        "%I:%M %p on %d %B",
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
        "%b %d %Y %I:%M %p",
        "%B %d %Y %I:%M %p",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %I:%M %p",
    ]

    for fmt in formats:
        try:
            if "%Y" not in fmt:
                parsed = dt.datetime.strptime(
                    f"{s} {now.year}", f"{fmt} %Y"
                )
            else:
                parsed = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue

        time_only = not any(
            token in fmt for token in ("%b", "%B", "%d", "%Y")
        )

        if time_only:
            parsed = parsed.replace(month=now.month, day=now.day)

        # Interpret naive datetime as system local time.
        parsed = parsed.astimezone()

        # Bare "10:32 PM" means the next such local time.
        if time_only and parsed <= now:
            parsed = (parsed.replace(tzinfo=None) + dt.timedelta(days=1)).astimezone()

        return parsed

    raise ValueError(f"could not parse absolute reset time: {s!r}")


class LimitWatcher:
    def __init__(self) -> None:
        self.buf = ""

    def feed(self, chunk: str) -> tuple[dt.datetime, str, int] | None:
        self.buf = (self.buf + strip_ansi(chunk))[-20000:]
        now = dt.datetime.now().astimezone()

        m = LIMIT_AT_RE.search(self.buf)
        if m:
            raw = m.group(1).strip().rstrip(" .)")
            return parse_absolute_time(raw, now), raw, 0

        m = LIMIT_IN_RE.search(self.buf)
        if m:
            raw = m.group(1).strip().rstrip(" .)")
            return parse_absolute_time(raw, now), raw, 0

        m = STATUS_RE.findall(self.buf)
        
        if len(m) == 0:
            return None
        
        percent_left = max([int(time) for (time, _) in m])
        
        earliest_time = min([(parse_absolute_time(raw, now), raw) for (_, raw) in m])

        return *earliest_time, percent_left

    def reset(self) -> None:
        self.buf = ""
        
class EnteredChatWatcher:
    def __init__(self) -> None:
        self.buf = ""

    def feed(self, chunk: str) -> bool:
        self.buf = (self.buf + strip_ansi(chunk))[-20000:]
        return self.buf.find("Ask Codex to do anything") != -1

    def reset(self) -> None:
        self.buf = ""
        
def term_size() -> tuple[int, int]:
    try:
        size = os.get_terminal_size()
        return size.lines, size.columns
    except OSError:
        print("failed to get terminal size")
        return 24, 80

def parse_codex_args(codex_args: list[str]) -> list[str]:
    args = list(codex_args)
    if args and args[0] == "--":
        args = args[1:]
    return args


class CodexWatcher:
    def __init__(self, codex_process: pexpect.spawn, continue_prompt: str):
        self.continue_prompt = continue_prompt
        self.auto_status = True
        self.auto_status_interval_ms = 1 * 60_000
        self.entered_chat_debounce_ms = 1_000

        self.codex_process = codex_process
        self.limit_watcher = LimitWatcher()
        self.entered_chat_watcher = EnteredChatWatcher()
        self.stdin_fd = sys.stdin.fileno()
        self.stdout_fd = sys.stdout.fileno()

        self.entered_chat_at = None
        self.needs_reprompt = False
        self.reprompt_at = None
        self.next_auto_status = dt.datetime.now().astimezone()

    def parse_codex_output(self):
        try:
            data = os.read(self.codex_process.child_fd, 8192)
        except OSError:
            return

        if not data:
            return

        os.write(self.stdout_fd, data)

        text = data.decode("utf-8", errors="replace")
        detected = self.limit_watcher.feed(text)
        
        if not self.entered_chat_at:
            entered_chat = self.entered_chat_watcher.feed(text)
            if entered_chat:
                # its hacky, yeah. but it works!
                self.entered_chat_at = dt.datetime.now().astimezone()

        if not detected:
            return

        reset_at, raw, percent_remaining = detected
        now = dt.datetime.now().astimezone()
        sleep_s = max(0.0, (reset_at - now).total_seconds())
        
        msg = f"[codex-continue] {percent_remaining}% remaining. next reset is in {sleep_s/60:.2} minutes at {reset_at.isoformat()}, then injecting: {self.continue_prompt!r}\n"
        os.write(self.stdout_fd, msg.encode("utf-8"))

        self.limit_watcher.reset()
        self.reprompt_at = reset_at
        self.needs_reprompt = percent_remaining == 0

    def parse_terminal_input(self):
        try:
            data = os.read(self.stdin_fd, 8192)
        except OSError:
            return
        try:
            os.write(self.codex_process.child_fd, data)
        except OSError:
            return

    def send_codex_input(self, data: str):
        try:
            os.write(self.codex_process.child_fd, data.encode("utf-8"))
            # self.codex_process.sendline(data)
            os.write(self.codex_process.child_fd, b"\r\n\r")
        except OSError:
            return

    def run(self) -> int:
        old_term_cfg = None

        if sys.stdin.isatty():
            old_term_cfg = termios.tcgetattr(self.stdin_fd)
            tty.setraw(self.stdin_fd)

        try:
            while True:
                now = dt.datetime.now().astimezone()
                new_win_size = term_size()

                prev_win_size = self.codex_process.getwinsize()

                if (
                    prev_win_size[0] != new_win_size[0]
                    or prev_win_size[1] != new_win_size[1]
                ):
                    self.codex_process.setwinsize(*term_size())

                read_fds = [self.codex_process.child_fd]

                if sys.stdin.isatty():
                    read_fds.append(self.stdin_fd)

                readable, _, _ = select.select(read_fds, [], [], 0.2)

                if self.auto_status and self.entered_chat_at and now > self.entered_chat_at + dt.timedelta(milliseconds=self.entered_chat_debounce_ms) and now > self.next_auto_status:
                    self.send_codex_input("/status")
                    self.next_auto_status = now + dt.timedelta(milliseconds=self.auto_status_interval_ms)

                if self.needs_reprompt and self.reprompt_at and now > self.reprompt_at:
                    self.send_codex_input(self.continue_prompt)

                if self.codex_process.child_fd in readable:
                    self.parse_codex_output()

                if self.stdin_fd in readable:
                    self.parse_terminal_input()

                if not self.codex_process.isalive():
                    break

        finally:
            if old_term_cfg is not None:
                termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, old_term_cfg)

        self.codex_process.close()
        return (
            self.codex_process.exitstatus
            if self.codex_process.exitstatus is not None
            else 0
        )


def main() -> int:
    ap = argparse.ArgumentParser(prog="codex-continue")
    ap.add_argument(
        "--continue-prompt",
        "-p",
        default="/goal resume",
        help="Text to inject after the reset time.",
    )
    ap.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="Arguments to pass to codex.",
    )

    args = ap.parse_args()
    codex_args = parse_codex_args(args.codex_args)

    print(f"[codex-continue] running: codex {shlex.join(codex_args)}", file=sys.stderr)

    codex_process = pexpect.spawn(
        "codex",
        codex_args,
        dimensions=term_size(),
    )

    codex_watcher = CodexWatcher(codex_process, args.continue_prompt)

    return codex_watcher.run()


if __name__ == "__main__":
    raise SystemExit(main())
