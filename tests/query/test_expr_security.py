"""Every sandbox-escape attempt must be rejected at parse. This is the file that says so.

A conditional breakpoint's condition is user-supplied source that (day 34) arrives over
HTTP. If any of these compiled, the evaluator would be a remote code execution primitive.
The whitelist rejects each one before evaluation -- and, as a second wall, the values it
would run against are captured data with no live object to reach anyway.
"""

from __future__ import annotations

import pytest

from chronotrace.query.expr import ConditionError, compile_condition

ESCAPES = [
    "__import__('os')",
    "__import__('os').system('rm -rf ~')",
    "os.system('id')",  # a Call -- rejected even though `os` is just an unbound name
    "eval('1')",
    "exec('x=1')",
    "open('/etc/passwd')",
    "globals()",
    "().__class__",
    "().__class__.__bases__[0]",
    "().__class__.__bases__[0].__subclasses__()",
    "x.__class__",
    "x.__dict__",
    "x._private",
    "lambda: 1",
    "[c for c in range(10)]",
    "{k: 1 for k in x}",
    "(walrus := 5)",
    "f'{x}'",
    "[].append(1)",
    "{}.update({})",
    "1 if x else 2",  # IfExp is not in the grammar
    "x[1:2]",  # slices are not in the grammar
    "x[::2]",
]


@pytest.mark.parametrize("source", ESCAPES)
def test_every_escape_is_rejected_at_parse(source: str) -> None:
    """None of these ever reaches evaluation -- `compile_condition` refuses them all."""
    with pytest.raises(ConditionError):
        compile_condition(source)


ALLOWED = [
    "i > 100",
    "a and b or not c",
    "0 < i <= 10",
    "x[0] == 1",
    "p.value > 5",
    "i in [1, 2, 3]",
    "total % 2 == 0",
    "flag",
]


@pytest.mark.parametrize("source", ALLOWED)
def test_the_grammar_still_admits_real_conditions(source: str) -> None:
    """The whitelist is tight but not useless -- ordinary debugging conditions compile."""
    compile_condition(source)  # must not raise
