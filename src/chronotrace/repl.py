"""A stepping REPL: time travel you can *feel*, before the UI exists (day 35).

Deliberately ugly and deliberately tiny. It exists to answer one question a design
document cannot -- does stepping backward through a real program feel right? -- and to
be the thing pointed at when someone asks what ChronoTrace does. The web UI will replace
every line of it; nothing here is load-bearing for the product.

It is also the first consumer of the stepping layer, which is the point: a debugger's
commands should fall out of `reconstruct` + `seek` with no new machinery, and if they
had not, the design would have been wrong.

The command table
-----------------
The six movement commands are one dict mapping a key to `(operation, direction)`, so
forward and backward are visibly the *same function with the opposite sign*. That is the
day's architectural claim, stated where it cannot rot.

`p` is overloaded -- bare `p` steps to the previous line, `p x` prints `x` -- because
that is the shape a debugger user's fingers already know (`p` for print, and backward is
the operation you reach for constantly here). Arity disambiguates.
"""

from __future__ import annotations

from collections.abc import Mapping

from chronotrace.reconstruct import (
    Direction,
    Edge,
    KeyframeReconstructor,
    MissingValue,
    ProgramState,
    ValueResolver,
    step,
    step_out,
    step_over,
)
from chronotrace.store import ChronoReader

MOVES = {
    "n": (step, Direction.FORWARD),
    "p": (step, Direction.BACKWARD),
    "o": (step_over, Direction.FORWARD),
    "O": (step_over, Direction.BACKWARD),
    "f": (step_out, Direction.FORWARD),
    "F": (step_out, Direction.BACKWARD),
}

HELP = """  n  next line          p  previous line
  o  step over          O  step over, backward
  f  finish this frame  F  back to where this frame was called
  g <seq>  jump to an instant     bt  backtrace
  p <var>  print a variable       q   quit"""

MAX_VALUE_CHARS = 200
"""How much of a captured value to show. The capture policy already bounds the value;
this bounds the *line*, so one big dict cannot scroll the position off the screen."""


class Repl:
    """A stepping session over one recording.

    Holds the reconstructor (and therefore its locality cache), so a drag of `n`/`p`
    keeps hitting the cache rather than restarting from a keyframe each time.

    `names` and `codes` are pre-resolved id -> text maps supplied by the caller. They are
    passed in rather than read from a recorder because the reconstruct layer speaks ids
    and only the caller knows where the strings came from -- and today they come from the
    live recorder, since the `.chrono` format does not yet persist its intern tables
    (issue #6). A recording opened from disk gets empty maps and renders raw ids.
    """

    __slots__ = ("_by_name", "_codes", "_names", "_reader", "_recon", "_resolver", "_seq")

    def __init__(
        self,
        reader: ChronoReader,
        *,
        names: Mapping[int, str] | None = None,
        codes: Mapping[int, str] | None = None,
    ) -> None:
        self._reader = reader
        self._recon = KeyframeReconstructor(reader)
        self._resolver = ValueResolver(reader)
        self._names = names or {}
        self._codes = codes or {}
        self._by_name = {name: name_id for name_id, name in self._names.items()}
        self._seq = 0

    @property
    def seq(self) -> int:
        """The instant the session is parked at."""
        return self._seq

    def run(self) -> None:
        """Read commands until EOF or `q`. The only place that touches stdin."""
        self._say(f"{len(self._reader):,} events. `?` for help, `q` to quit.")
        if self._reader.truncated:
            self._say("warning: this recording is truncated -- its tail was lost.")
        self._say(self._where())
        while True:
            try:
                line = input("(chrono) ")
            except (EOFError, KeyboardInterrupt):
                self._say("")
                return
            if not self.do(line):
                return

    def do(self, line: str) -> bool:
        """Run one command. Returns False to end the session.

        Split from `run` so tests drive the REPL without a terminal -- the command
        semantics are worth testing, the input loop is not.
        """
        cmd, _, arg = line.strip().partition(" ")
        arg = arg.strip()
        if cmd in {"q", "quit"}:
            return False
        if cmd == "p" and arg:
            self._print_var(arg)
        elif cmd in MOVES:
            self._move(cmd)
        elif cmd == "bt":
            self._backtrace()
        elif cmd == "g":
            self._goto(arg)
        elif cmd in {"?", "h", "help"}:
            self._say(HELP)
        elif cmd:
            self._say(f"unknown command {cmd!r} -- `?` for help")
        return True

    # -- movement -----------------------------------------------------------

    def _move(self, cmd: str) -> None:
        """One stepping command: find the destination `seq`, then reconstruct once there."""
        operation, direction = MOVES[cmd]
        dest = operation(self._reader, self._seq, direction)
        if isinstance(dest, Edge):
            self._say(f"-- {dest.value}")
            return
        self._seq = dest
        self._say(self._where())

    def _goto(self, arg: str) -> None:
        if not arg.lstrip("-").isdigit() or not 0 <= int(arg) < len(self._reader):
            self._say(f"g needs an instant in [0, {len(self._reader)})")
            return
        self._seq = int(arg)
        self._say(self._where())

    # -- inspection ---------------------------------------------------------

    def _backtrace(self) -> None:
        """The live frames, innermost first, with the current one starred.

        A suspended generator is listed too, and marked: it is live, holds live locals,
        and is on no stack -- the day-6 registry model made visible.
        """
        state = self._state()
        for frame in reversed(state.frames):
            mark = "*" if frame.frame_id == state.current_frame_id else " "
            suspended = "  (suspended)" if frame.suspended else ""
            self._say(
                f"{mark} #{frame.frame_id} {self._code(frame.code_id)}:{frame.lineno}{suspended}"
            )
        if state.exception is not None:
            self._say(f"  exception in flight, raised at seq {state.exception.raised_at_seq}")

    def _print_var(self, name: str) -> None:
        """Print one local of the current frame, as it was at this instant."""
        state = self._state()
        frame = state.frame(state.current_frame_id)
        name_id = self._by_name.get(name)
        if frame is None or name_id is None or name_id not in frame.bindings:
            self._say(f"{name} is not bound in this frame at seq {self._seq}")
            return
        try:
            value = self._resolver.resolve(frame.bindings[name_id])
        except MissingValue as exc:
            self._say(f"{name}: {exc}")
            return
        text = repr(value)
        if len(text) > MAX_VALUE_CHARS:
            text = f"{text[:MAX_VALUE_CHARS]}... ({len(text)} chars)"
        self._say(f"{name} = {text}")

    def _where(self) -> str:
        """The position line.

        At a `RETURN`/`UNWIND` instant the frame that ran the event is already gone from
        the state, so there is a current frame *id* with no live frame behind it. Falling
        back to the innermost caller and saying so beats printing "no live frame" while
        `bt` shows three -- the state is not empty, the frame just ended here.
        """
        state = self._state()
        frame = state.frame(state.current_frame_id)
        if frame is not None:
            return f"[{self._seq}] {self._code(frame.code_id)}:{frame.lineno}"
        if not state.frames:
            return f"[{self._seq}] no live frame"
        caller = state.frames[-1]
        return (
            f"[{self._seq}] {self._code(caller.code_id)}:{caller.lineno}"
            "  (the frame that ran this instant has exited)"
        )

    def _state(self) -> ProgramState:
        return self._recon.reconstruct(self._seq)

    def _code(self, code_id: int) -> str:
        return self._codes.get(code_id, f"code#{code_id}")

    def _say(self, text: str) -> None:
        """Every line the REPL emits, so output is in one place -- and so is the lint."""
        print(text)  # noqa: T201 -- a REPL's entire job is printing
