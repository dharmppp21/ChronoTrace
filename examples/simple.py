"""The smallest program whose event stream a human can verify by hand.

Every line is here for a reason and the line numbers are load-bearing:
`tests/recorder/test_recorder.py` asserts the exact `(kind, lineno)` sequence this
produces. **Editing this file changes that golden test.** That is the point --
the test is a human's expectation written down, and it is worth more than any
mock, because a mock would only prove the recorder agrees with itself.

Shape, deliberately:
* a plain call (`double`)          -> CALL / LINE / RETURN
* a nested call (`quadruple`)      -> a call inside a call, so the stack has depth
* a loop with an accumulator       -> the same lines repeating, which is what makes
                                      a timeline look like a timeline
"""


def double(n: int) -> int:
    result = n * 2
    return result


def quadruple(n: int) -> int:
    once = double(n)
    twice = double(once)
    return twice


def main() -> int:
    total = 0
    for i in range(2):
        total += quadruple(i)
    return total


if __name__ == "__main__":
    print(main())
