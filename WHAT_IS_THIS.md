# What is ChronoTrace?

**A debugger that lets you rewind your program.**

---

## The problem it solves

Your program prints a wrong number. So you add a `print()` to find out why, and run it again.

This time it works. Or it breaks somewhere else. Or the run takes four minutes and you get one
new line of information out of it, so you add another `print()` and wait another four minutes.

That is the normal debugging loop: **guess where to look, run again, hope it happens again.**

The trouble is that the mistake already happened. It happened before you noticed anything was
wrong, and by the time you see the bad number, the moment that caused it is gone.

## What ChronoTrace does

It **records your program once, while it runs.** After that you can move around inside that
recording — forwards, and more importantly *backwards* — as many times as you want, without
running the program again.

Think of a football match. You see the ball in the net and you want to know how the goal
started. You do not ask the players to play the match again. You rewind the tape.

ChronoTrace is the tape for your program.

## What that lets you do

- **Step backwards.** Not just "run to here" — actually go back a line, or back into the
  function that called this one.
- **Ask where a value came from.** "This total is 120. What set it to 120, and what was it
  before?" You get the exact moment, not a guess.
- **Put a breakpoint in the past.** You forgot to set one on line 42? Set it *now*, after the
  program has finished, and see every time it would have hit. There is nothing to re-run,
  because the run already happened.
- **Watch one variable's whole life** in a single list, from first value to last.

## Try it in three commands

```bash
pip install "chronotrace[ui]"
```

```bash
chronotrace record --ui examples/stale_discounts.py
```

That runs the program, records it, and opens a browser. Drag the timeline and the code,
variables and function calls all move with you.

Prefer the terminal? Record it, then just ask questions:

```bash
chronotrace record examples/stale_discounts.py
chronotrace query --watch total
```

The example is a real bug: three customer orders, and the third one is charged the wrong
price. Nothing crashes, and the code looks fine. Scrub backwards and you can watch the
mistake happen.

No install at all, if you just want a look:

```bash
docker run --rm -p 8000:8000 ghcr.io/dharmppp21/chronotrace
```

## When to use it

**Good for:** a bug you are already stuck on. Especially one where the wrong thing appears
long after the mistake, or one that is annoying to reproduce.

**Not good for:** leaving switched on. Recording every value makes a program much slower — it
is a tool you reach for when you are stuck, not something that runs all the time. It is also
not for production: a recording contains everything your program had in memory, passwords
included, so treat a `.chrono` file like a private file, and never attach one to a bug report.

## In one line

Normal debugging asks *"can I make it happen again?"*
ChronoTrace asks *"what happened?"* — and it already knows, because it was recording.

---

Longer docs: [README.md](README.md) for the full tour, [docs/](docs/) for details,
[CHANGELOG.md](CHANGELOG.md) for what changed.
