# Pinned environment for reproducing the benchmark suite.
#
#   docker build -t chronotrace-bench -f benchmarks/Dockerfile .
#   docker run --rm chronotrace-bench            # -> python -m benchmarks --quick
#   docker run --rm chronotrace-bench --reps 7   # override the default args
#
# The point of pinning is that "reproducible within a few percent" is only a claim a
# reader can check if the interpreter version and dependency set are fixed. Numbers
# from this Linux image will differ from the Windows numbers in RESULTS.md (timer
# resolution and scheduler behaviour differ) -- that is expected and is why the OS is
# recorded in the generated header.
#
# Digest-pin the base for true reproducibility once you have a digest; the tag is the
# lazy version that still fixes the Python minor.  # ponytail: tag-pinned, digest-pin when it matters
FROM python:3.14-slim

WORKDIR /app

# coverage.py is a *benchmark baseline* here, not a runtime dep of the package, so it
# is installed explicitly rather than pulled from the package's extras.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e . coverage

# The workloads live in spikes/ and the suite in benchmarks/; both are needed to run.
COPY spikes ./spikes
COPY benchmarks ./benchmarks
COPY docs ./docs

# --quick by default so a bare `docker run` finishes in a couple of minutes; override
# with any `python -m benchmarks` flags after the image name.
ENTRYPOINT ["python", "-m", "benchmarks"]
CMD ["--quick"]
