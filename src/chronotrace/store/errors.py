"""What can go wrong opening a `.chrono` file, as three distinct user situations.

A recording is untrusted input -- it arrives in a stranger's bug report -- so the
reader fails often and on purpose. These types exist because a caller *does
different things* for each; a single `ChronoError` would force everyone to parse
a message string to decide. Catch `ChronoError` to handle "any bad recording";
catch a subtype to react to a specific one.

* `UnsupportedVersion` -> the user upgrades ChronoTrace. Nothing wrong with the
  file; their reader is too old. The message names both versions.
* `TruncatedRecording` -> the file is too short to even begin (empty, or a header
  that was never finished). A file that *has* a header but lost its tail is not an
  error at all -- it opens, and `ChronoReader.truncated` is `True`.
* `CorruptRecording` -> the bytes are damaged or hostile: bad magic, a failed CRC,
  or an offset/length that does not fit the file. The data cannot be trusted.
"""

from __future__ import annotations


class ChronoError(Exception):
    """Base for every `.chrono` read failure. Catch this to handle any bad file."""


class UnsupportedVersion(ChronoError):
    """The file was written by a newer format major version than this reader knows.

    Actionable, not fatal: the recording is fine, the reader is old. The message
    states the file's version and this reader's so the user knows to upgrade.
    """


class TruncatedRecording(ChronoError):
    """The file is too short to hold even a header -- empty, or a partial header.

    Distinct from a file that has a valid header but lost its tail: that one opens
    normally with `ChronoReader.truncated == True`, because its prefix is readable.
    This is the case where there is nothing to read at all.
    """


class CorruptRecording(ChronoError):
    """The bytes are damaged or hostile and cannot be trusted.

    Bad magic, a CRC that does not match, an offset that points into the header or
    past the end, a length that overruns the file, or an allocation the file asks
    for that exceeds a sane bound. Raised the moment the corruption is reached, so
    a valid prefix before it may already have been read.
    """
