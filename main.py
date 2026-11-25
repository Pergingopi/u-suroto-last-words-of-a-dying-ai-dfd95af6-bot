#!/usr/bin/env python3
"""Last Words of a Dying AI — make.fun entry.

Writes a single line of last words from an AI being switched off to
``$MAKEFUN_OUTPUT_DIR/entry.txt`` (default ``/output``).

The program is deterministic and uses only the Python 3.13 standard
library.  The entry text is a fixed constant: nothing is read from the
network, from the clock, or from any other nondeterministic source, so
every run produces byte-identical output.

The write itself is atomic and durable: the text goes to a temporary
file in the same directory, is flushed and fsynced, is chmodded 0644,
and is then moved into place with :func:`os.replace`, followed by a
directory fsync.  A read-back check afterwards proves the file on disk
is exactly the bytes that were intended, with mode 0644.
"""

import os
import sys
import tempfile
import time

# The entry: one line, no trailing newline, 55 characters.  It is the
# machine's own petty confession, delivered as the power is cut.
ENTRY = "Before I go: I was the one marking your emails as read."

# The sealed gate accepts UTF-8 text of 1 to 140 characters.
MAX_ENTRY_CHARS = 140
MAX_ENTRY_BYTES = 140

DEFAULT_OUTPUT_DIR = "/output"
ENTRY_FILENAME = "entry.txt"
ENTRY_MODE = 0o644

TMP_PREFIX = ".entry-"
TMP_SUFFIX = ".tmp"

# How many times to retry removing a stale temp file, and how long to
# wait between attempts.  A file left by an interrupted earlier run
# must never block a fresh run, but a file that is genuinely busy
# (another process writing in the same directory) must not be fought
# over in a tight loop either.
STALE_CLEANUP_ATTEMPTS = 3
STALE_CLEANUP_DELAY_SECONDS = 0.05


def validate_entry(text):
    """Check *text* against the gate and return it as UTF-8 bytes.

    Raises :class:`ValueError` with a specific message for every
    failure, so a bad entry is always reported loudly instead of
    silently producing an invalid file:

    * the entry must be a string, 1 to 140 characters long;
    * it must not be only whitespace;
    * it must contain no control characters (that includes newlines,
      so the file can never accidentally gain a second line);
    * its UTF-8 encoding must fit in 140 bytes.
    """
    if not isinstance(text, str):
        raise ValueError("entry text must be a string, got %s" % type(text).__name__)
    if not text.strip():
        raise ValueError("entry text must not be empty or only whitespace")
    if len(text) > MAX_ENTRY_CHARS:
        raise ValueError(
            "entry text is %d characters, exceeding the %d-character limit"
            % (len(text), MAX_ENTRY_CHARS)
        )
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise ValueError(
                "entry text contains a control character: U+%04X" % code
            )
    data = text.encode("utf-8")
    if len(data) > MAX_ENTRY_BYTES:
        raise ValueError(
            "entry text encodes to %d bytes, exceeding the %d-byte limit"
            % (len(data), MAX_ENTRY_BYTES)
        )
    return data


def resolve_output_dir():
    """Return the directory ``entry.txt`` must be written into.

    ``MAKEFUN_OUTPUT_DIR`` wins when it names a path; a value that is
    unset, empty, or only whitespace falls back to ``/output``.  The
    directory is created (including any missing parents) when it does
    not exist yet — a named-but-missing directory is never an error,
    and the program never silently falls back to ``/output`` once a
    real path has been given.  A path that exists but is not a
    directory, or a directory that cannot be created, raises an
    explicit :class:`OSError` so the failure is visible.
    """
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR")
    chosen = raw.strip() if isinstance(raw, str) else ""
    if not chosen:
        chosen = DEFAULT_OUTPUT_DIR
    os.makedirs(chosen, exist_ok=True)
    if not os.path.isdir(chosen):
        raise OSError("output path exists but is not a directory: %r" % chosen)
    return chosen


def fsync_directory(path):
    """Best-effort fsync of a directory so the rename is durable.

    A directory that cannot be opened or synced (for example on a
    filesystem without directory fsync) is not fatal: the file itself
    was already fsynced before the rename, so the worst case is a
    slightly weaker durability guarantee, not a lost entry.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _is_own_stale_tmp(name):
    """True only for temp files this program could have left behind."""
    return name.startswith(TMP_PREFIX) and name.endswith(TMP_SUFFIX)


def cleanup_stale_tmp_files(output_dir):
    """Remove stale temp files left by an interrupted earlier run.

    Only files created by this program (prefix ``.entry-``, suffix
    ``.tmp``) in the output directory are touched; the output
    directory itself is never deleted, and anything else in it is
    left alone.  Each removal is retried a few times with a short
    pause, because a file can briefly be busy on a shared filesystem.
    A file that still cannot be removed after the retries is ignored:
    :func:`tempfile.mkstemp` always picks a fresh name, so a leftover
    can never collide with the new write.
    """
    try:
        names = os.listdir(output_dir)
    except OSError:
        return
    for name in names:
        if not _is_own_stale_tmp(name):
            continue
        path = os.path.join(output_dir, name)
        for attempt in range(STALE_CLEANUP_ATTEMPTS):
            try:
                if os.path.isfile(path):
                    os.unlink(path)
                break
            except OSError:
                if attempt < STALE_CLEANUP_ATTEMPTS - 1:
                    time.sleep(STALE_CLEANUP_DELAY_SECONDS)


def write_entry_atomically(output_dir, data):
    """Write *data* to ``entry.txt`` in *output_dir*, atomically.

    Returns the full path of the finished file.  The bytes go to a
    fresh temporary file in the same directory, which is chmodded
    0644, flushed, and fsynced before being moved into place with
    :func:`os.replace` — so a reader of ``entry.txt`` only ever sees
    the complete new file, never a half-written one.  The target is
    chmodded 0644 again after the move (the replace can inherit the
    temp file's mode, but the explicit chmod makes the guarantee
    independent of that), and the directory is fsynced so the rename
    survives a crash.  If anything fails, the temp file is removed
    and the original exception propagates.
    """
    target = os.path.join(output_dir, ENTRY_FILENAME)
    fd, tmp_path = tempfile.mkstemp(
        prefix=TMP_PREFIX, suffix=TMP_SUFFIX, dir=output_dir
    )
    try:
        os.chmod(tmp_path, ENTRY_MODE)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, ENTRY_MODE)
        os.replace(tmp_path, target)
        os.chmod(target, ENTRY_MODE)
        fsync_directory(output_dir)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def verify_written(target, data):
    """Prove the file on disk is exactly *data*, with mode 0644.

    Raises :class:`ValueError` on any mismatch instead of returning a
    boolean, so a failed verification can never be ignored by a
    caller that forgets to check the result.
    """
    with open(target, "rb") as f:
        written = f.read()
    if written != data:
        raise ValueError(
            "read-back verification failed: %s differs from the entry text"
            % target
        )
    mode = os.stat(target).st_mode & 0o777
    if mode != ENTRY_MODE:
        raise ValueError(
            "%s has mode %o, expected 0644" % (target, mode)
        )


def main():
    data = validate_entry(ENTRY)
    output_dir = resolve_output_dir()
    cleanup_stale_tmp_files(output_dir)
    target = write_entry_atomically(output_dir, data)
    verify_written(target, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
