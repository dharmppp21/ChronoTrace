"""A Hypothesis grammar for small Python programs the recorder must survive.

Day 22's referee is only as good as the programs pointed at it, and until today those
were five examples a human thought to write. Humans write the code they already have in
mind; the bugs live in the code they do not. This generates programs nobody would think
to write, and hands each one to the referee.

Why a grammar over structure, not over text
-------------------------------------------
Generating characters and hoping they parse wastes essentially every draw -- and worse,
Hypothesis would then *shrink* towards shorter strings rather than simpler programs, so a
failure would minimise to unreadable rubble. The grammar produces a tree whose nesting is
structural, so the renderer cannot emit bad indentation and every draw is valid Python.
The tree and the scope model it is built against live in `syntax.py`.

Valid by construction, in three senses
--------------------------------------
**Syntactically**, by the tree above. **Semantically**, because generation is
context-sensitive: an `Env` threads through every production carrying which names are
bound, which are `nonlocal`-able, and what type each holds, so a name is only ever
referenced where it exists and only combined with operators its type supports. A
context-free grammar cannot express "`nonlocal x` requires x bound in an enclosing
function scope", which is exactly why the multi-scope idioms below are single productions
rather than something the grammar might stumble into.

**Terminating and deterministic**, which is not fussiness -- it is what makes the campaign
runnable at all. A generated `while` that does not stop hangs CI with no failing example
to look at, and a program whose output depends on hash ordering or the clock produces
failures that vanish when you rerun them, which costs days. So loops are only
`for _ in range(k)` with `k` a small literal, recursion always carries a guarded
countdown, and there is no I/O, no clock, no `id()`, no randomness, and no `set`
(iteration order under hash randomisation). Bounded **structurally**, never by a timeout:
a timeout tells you the program hung, while a grammar that cannot express non-termination
means it never does.

Types, and why only two
-----------------------
`INT` and `LIST` (of ints). Every operator is total on its operands -- `+` and `*` on
ints, `len()` and `+ [n]` on lists -- so a generated program cannot raise by accident.
Indexing is deliberately absent: `lst[0]` on an empty list is an `IndexError` the grammar
did not mean to generate, and the exception idioms below raise on purpose instead. That
distinction matters, because an accidental exception would be indistinguishable from a
recorder bug in the campaign's output.

What is deliberately out of scope
---------------------------------
* **Threads** -- the recorder is single-threaded today and `ProgramState` carries no
  thread dimension; generating them would test a claim the system does not make.
* **`eval`/`exec`** -- creates code objects belonging to no file, which the scope filter
  cannot attribute and the intern table would grow without bound.
* **C extensions** -- no Python frames, so nothing to record.
* **Imports** -- the stdlib is out of scope by design, so an import adds no recordable
  frames, only nondeterministic side effects at module level.
* **`async`** -- covered by `examples/generators.py` in the referee already, and
  generating well-formed coroutines needs an event-loop harness around every example.
"""

from __future__ import annotations

import itertools
from dataclasses import replace

from hypothesis import strategies as st

from .syntax import Block, Env, Line, Stmt, render

MAX_DEPTH = 3
"""Nesting budget. Three is enough for a closure two levels up (the deepest construct the
edge-case list names) and keeps a shrunk failure readable."""

LOOP_BOUND = 3
"""Largest `range()` a generated loop may take. Small on purpose: the campaign wants many
*shapes*, and a long loop only repeats one shape while multiplying recording cost."""


# -- expressions: total on their operands, so nothing raises by accident ----------------


@st.composite
def _int_expr(draw: st.DrawFn, env: Env) -> str:
    """An int-valued expression. Every branch is total; none can raise."""
    options = [st.integers(0, 9).map(str)]
    if env.ints:
        options.append(st.sampled_from(env.ints))
        options.append(
            st.tuples(st.sampled_from(env.ints), st.sampled_from("+*-"), st.integers(1, 5)).map(
                lambda t: f"({t[0]} {t[1]} {t[2]})"
            )
        )
    if env.lists:
        options.append(st.sampled_from(env.lists).map(lambda n: f"len({n})"))
    if env.funcs:
        options.append(st.sampled_from(env.funcs).map(lambda f: f"{f}(2)"))
    text: str = draw(st.one_of(*options))
    return text


@st.composite
def _list_expr(draw: st.DrawFn, env: Env) -> str:
    """A list-valued expression, including a comprehension (its own implicit scope)."""
    options = [
        st.lists(st.integers(0, 9), max_size=3).map(lambda xs: f"[{', '.join(map(str, xs))}]")
    ]
    if env.lists:
        options.append(st.sampled_from(env.lists).map(lambda n: f"{n} + [1]"))
    options.append(st.integers(1, LOOP_BOUND).map(lambda k: f"[_c * 2 for _c in range({k})]"))
    text: str = draw(st.one_of(*options))
    return text


# -- statements ------------------------------------------------------------------------
#
# Ordered simple-to-complex in `_statement`'s `one_of`, because Hypothesis shrinks towards
# earlier branches. That ordering is the difference between a failure that minimises to
# `v0 = 1; del v0` and one that minimises to a nest of classes and generators: shrink
# quality is not cosmetic, it decides whether diagnosing a found bug takes ten minutes or
# an afternoon.


@st.composite
def _assign(draw: st.DrawFn, env: Env) -> tuple[list[Stmt], Env]:
    name = env.fresh("v")
    deletable = (*env.deletable, name)
    if draw(st.booleans()):
        return [Line(f"{name} = {draw(_int_expr(env))}")], replace(
            env, ints=(*env.ints, name), deletable=deletable
        )
    return [Line(f"{name} = {draw(_list_expr(env))}")], replace(
        env, lists=(*env.lists, name), deletable=deletable
    )


@st.composite
def _augment(draw: st.DrawFn, env: Env) -> tuple[list[Stmt], Env]:
    target = draw(st.sampled_from(env.ints))
    return [Line(f"{target} += {draw(_int_expr(env))}")], env


@st.composite
def _delete(draw: st.DrawFn, env: Env) -> tuple[list[Stmt], Env]:
    """`del x`, optionally rebinding afterwards -- an edge case the recorder is blind to."""
    target = draw(st.sampled_from(env.deletable))
    out: list[Stmt] = [Line(f"del {target}")]
    without = replace(
        env,
        ints=tuple(n for n in env.ints if n != target),
        lists=tuple(n for n in env.lists if n != target),
        deletable=tuple(n for n in env.deletable if n != target),
    )
    if not draw(st.booleans()):
        return out, without
    # Rebinding changes the name's *type*, and the environment has to be told. A list
    # deleted and rebound to an int, with the environment still listing it under `lists`,
    # produces `len(v0)` on an int -- a TypeError the grammar never meant to generate.
    # Found only by the deep campaign: it needs a delete, a rebind, and a later `len`.
    out.append(Line(f"{target} = {draw(st.integers(0, 9))}"))
    return out, replace(
        without, ints=(*without.ints, target), deletable=(*without.deletable, target)
    )


@st.composite
def _conditional(draw: st.DrawFn, env: Env, depth: int) -> tuple[list[Stmt], Env]:
    test = f"{draw(_int_expr(env))} > {draw(st.integers(0, 5))}"
    body, _ = draw(_body(env.nested(), depth - 1))
    clauses: tuple[tuple[str, tuple[Stmt, ...]], ...] = ()
    if draw(st.booleans()):
        alt, _ = draw(_body(env.nested(), depth - 1))
        clauses = (("else:", tuple(alt)),)
    return [Block(f"if {test}:", tuple(body), clauses)], env


@st.composite
def _loop(draw: st.DrawFn, env: Env, depth: int) -> tuple[list[Stmt], Env]:
    """`for` over a literal `range` -- the only loop form, so termination is structural."""
    var = env.fresh("i")
    inner = replace(env.nested(), ints=(*env.ints, var), deletable=(var,))
    body, _ = draw(_body(inner, depth - 1))
    return [Block(f"for {var} in range({draw(st.integers(0, LOOP_BOUND))}):", tuple(body))], env


@st.composite
def _try_block(draw: st.DrawFn, env: Env, depth: int) -> tuple[list[Stmt], Env]:
    """try/except/finally, including the two shapes that trip interpreters up.

    A `raise` inside `finally` (which discards the in-flight exception) and a `return`
    inside `finally` (which discards the `try`'s return value) are both on the edge-case
    list, and both are one boolean away from the ordinary shape -- so they are drawn here
    rather than written out as separate templates.
    """
    body, _ = draw(_body(env.nested(), depth - 1))
    if draw(st.booleans()):
        body = [*body, Line("raise ValueError('generated')")]
    elif env.in_function and draw(st.booleans()):
        body = [*body, Line(f"return {draw(_int_expr(env))}")]
    handler, _ = draw(_body(env.nested(), depth - 1))
    clauses = [("except ValueError:", tuple(handler))]
    if draw(st.booleans()):
        final, _ = draw(_body(env.nested(), depth - 1))
        if draw(st.booleans()):
            final = [*final, _RAISE_IN_FINALLY]
        elif env.in_function and draw(st.booleans()):
            final = [*final, Line("return 0")]  # discards the try's return value
        clauses.append(("finally:", tuple(final)))
    return [Block("try:", tuple(body), tuple(clauses))], env


_RAISE_IN_FINALLY = Block(
    "try:",
    (Line("raise ValueError('from finally')"),),
    (("except ValueError:", (Line("pass"),)),),
)
"""An exception raised *inside* a `finally`, caught there.

Contained on purpose. Raising bare from a `finally` discards the in-flight exception and
escapes the enclosing `except`, so the generated program would die of an uncaught error --
indistinguishable, in the campaign's output, from the recorder crashing.
"""


@st.composite
def _function(draw: st.DrawFn, env: Env, depth: int) -> tuple[list[Stmt], Env]:
    """A nested `def`: possibly a generator, possibly `*args`/`**kwargs`, possibly with a
    mutable default argument that accumulates across calls.

    `enclosing` accumulates rather than replaces, which is what lets a doubly-nested
    function legally declare `nonlocal` against a name **two** levels up -- one of the
    edge cases, reached by composition instead of by a template. Module-level names are
    excluded from it, because those need `global`, not `nonlocal`.
    """
    name, param = env.fresh("f"), env.fresh("p")
    signature, extra = draw(
        st.sampled_from(
            [
                (param, ()),
                (f"{param}, *args, **kwargs", ()),
                (f"{param}, acc=[]", (Line(f"acc.append({param})"),)),  # mutable default
            ]
        )
    )
    inner = Env(
        ints=(param,),
        funcs=env.funcs,
        enclosing=env.enclosing + (env.ints if env.in_function else ()),
        in_function=True,
        deletable=(param,),
        supply=env.supply,
    )
    body, after = draw(_body(inner, depth - 1))
    generator = draw(st.booleans())
    # The body may have deleted the parameter, so the tail reads the environment as it
    # actually is rather than as it was at the signature.
    value = param if param in after.ints else "0"
    tail = Line(f"yield {value}") if generator else Line(f"return {value}")
    block = Block(f"def {name}({signature}):", (*extra, *body, tail))
    if generator:
        return [block], replace(env, gens=(*env.gens, name))
    return [block], replace(env, funcs=(*env.funcs, name))


@st.composite
def _nonlocal_write(draw: st.DrawFn, env: Env) -> tuple[list[Stmt], Env]:
    """A closure that writes to its enclosing scope -- a classic recorder blind spot."""
    target = draw(st.sampled_from(env.enclosing))
    name = env.fresh("f")
    inner = Block(
        f"def {name}():",
        (Line(f"nonlocal {target}"), Line(f"{target} += {draw(st.integers(1, 3))}")),
    )
    return [inner, Line(f"{name}()")], env


@st.composite
def _drive_generator(draw: st.DrawFn, env: Env) -> tuple[list[Stmt], Env]:
    """Call a generator and either exhaust it or walk away -- the abandoned-frame case.

    A generator dropped before exhaustion keeps a live frame that only dies when the
    collector finalises it, which is the shape that exposed the day-22 frame-id fusion.
    """
    gen = draw(st.sampled_from(env.gens))
    name = env.fresh("v")
    if draw(st.booleans()):
        return [Line(f"{name} = len(list({gen}(3)))")], replace(env, ints=(*env.ints, name))
    # `next(g, None)`, not `next(g)`: a generated generator may `return` before reaching
    # its `yield`, and a bare `next` would then raise StopIteration -- an accidental
    # exception, indistinguishable in the campaign from a recorder crash.
    return [Line(f"{name} = {gen}(3)"), Line(f"next({name}, None)")], env  # then abandoned


@st.composite
def _recursion(draw: st.DrawFn, env: Env) -> tuple[list[Stmt], Env]:
    """A self-recursive function, terminating by construction.

    The guard is part of the production rather than something the grammar might stumble
    into, because "recursive and terminating" is not a property a context-free grammar can
    promise -- and an infinite recursion would end the campaign with a `RecursionError`
    instead of a failing example.
    """
    name, result = env.fresh("f"), env.fresh("v")
    depth_arg = draw(st.integers(1, 4))
    block = Block(
        f"def {name}(n):",
        (
            Block("if n <= 0:", (Line("return 0"),)),
            Line(f"return n + {name}(n - 1)"),
        ),
    )
    return [block, Line(f"{result} = {name}({depth_arg})")], replace(
        env, ints=(*env.ints, result), funcs=(*env.funcs, name)
    )


@st.composite
def _shadow_global(draw: st.DrawFn, env: Env) -> tuple[list[Stmt], Env]:
    """Bind a local named exactly like the module global.

    On the edge-case list because the recorder reads `f_locals`: a local sharing a
    global's name is the case where "which binding is this?" has two answers.

    Writing *through* to the global with a `global` declaration is deliberately absent.
    Python requires the declaration before any other use of the name in that scope, which
    the grammar would have to track per scope to avoid emitting a SyntaxError -- and
    "a nested frame writes an outer frame's binding" is already covered by
    `_nonlocal_write`, which needs no such bookkeeping.
    """
    return [Line(f"{GLOBAL_NAME} = {draw(st.integers(0, 9))}")], replace(
        env, ints=(*env.ints, GLOBAL_NAME)
    )


@st.composite
def _class_def(draw: st.DrawFn, env: Env) -> tuple[list[Stmt], Env]:
    name = env.fresh("C")
    attr = draw(st.integers(0, 9))
    method = Block("def method(self):", (Line(f"return self.value + {attr}"),))
    body = (Line(f"value = {attr}"), method)
    instance = env.fresh("v")
    return [
        Block(f"class {name}:", body),
        Line(f"{instance} = {name}().method()"),
    ], replace(env, ints=(*env.ints, instance))


@st.composite
def _body(draw: st.DrawFn, env: Env, depth: int) -> tuple[list[Stmt], Env]:
    """A non-empty statement list, threading the environment left to right."""
    out: list[Stmt] = []
    for _ in range(draw(st.integers(1, 3))):
        stmts, env = draw(_statement(env, depth))
        out.extend(stmts)
    return out, env


@st.composite
def _statement(draw: st.DrawFn, env: Env, depth: int) -> tuple[list[Stmt], Env]:
    """One statement, from the productions the current scope makes legal."""
    options = [_assign(env)]
    if env.ints:
        options.append(_augment(env))
    if env.deletable:
        options.append(_delete(env))
    if env.gens:
        options.append(_drive_generator(env))
    if depth > 0:
        options.extend([_conditional(env, depth), _loop(env, depth), _try_block(env, depth)])
        options.extend([_function(env, depth), _class_def(env), _recursion(env)])
        options.append(_shadow_global(env))
        if env.enclosing:
            # Weighted up by repetition, which is how `one_of` expresses a bias. Closures
            # writing to an enclosing scope need a function nested inside a function that
            # already bound something, and that compound condition put `nonlocal` at 8
            # programs in 400 -- too rare for the coverage assertion to hold, and too rare
            # to call the construct tested. Uniformity over productions is a default, not
            # a principle.
            options.extend([_nonlocal_write(env)] * 6)
    chosen: tuple[list[Stmt], Env] = draw(st.one_of(*options))
    return chosen


GLOBAL_NAME = "G"
"""The one module-level global, so `global`/shadowing productions have a target."""


@st.composite
def python_program(draw: st.DrawFn) -> str:
    """A complete, valid, terminating, deterministic module exposing `main()`.

    `main` is the entry point because that is what the day-22 referee records. Helper
    definitions sit at module level so the generated program has a call graph rather than
    one flat frame.

    Complexity: bounded by `MAX_DEPTH` and the per-body statement count -- a few dozen
    statements at most, which keeps a failing example readable and a recording small.
    """
    env = Env(supply=itertools.count())
    prelude: list[Stmt] = [Line(f"{GLOBAL_NAME} = 7")]
    for _ in range(draw(st.integers(0, 2))):
        stmts, env = draw(_function(env, MAX_DEPTH))
        prelude.extend(stmts)
    # `main` starts with one bound local, which is what makes closures reachable at all.
    # `nonlocal` needs a function nested inside a function that has *already bound
    # something*, and with `main` starting empty that compound condition left it in 8
    # programs per 400 -- so the construct was in the grammar and effectively untested.
    # A `main` whose first line binds a local is also the most ordinary shape there is.
    seed = env.fresh("v")
    inner = replace(env, in_function=True, ints=(seed,), deletable=(seed,))
    body, _ = draw(_body(inner, MAX_DEPTH))
    main = (Line(f"{seed} = {draw(st.integers(0, 9))}"), *body, Line("return 0"))
    module = [*prelude, Block("def main():", main)]
    return "\n".join(render(tuple(module))) + "\n"
