# SPDX-License-Identifier: MIT OR Apache-2.0
# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the MIT License.  See the LICENSE file in the root of this
# repository for complete details.

"""
Extract a structured traceback from an exception.

Based on work by Will McGugan
<https://github.com/hynek/structlog/pull/407#issuecomment-1150926246>`_ from
`rich.traceback
<https://github.com/Textualize/rich/blob/972dedff/rich/traceback.py>`_.
"""

from __future__ import annotations


__lazy_modules__ = ["traceback"]

import fnmatch
import os
import os.path
import sys

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from traceback import walk_tb
from types import ModuleType, TracebackType
from typing import Any, TypeAlias


try:
    import rich
    import rich.pretty
except ImportError:
    rich = None  # type: ignore[assignment]

from .typing import ExcInfo


__all__ = [
    "ExceptionDictTransformer",
    "Frame",
    "Stack",
    "SyntaxError_",
    "Trace",
    "extract",
    "safe_str",
    "to_repr",
]


SHOW_LOCALS = True
LOCALS_MAX_LENGTH = 10
LOCALS_MAX_STRING = 80
MAX_FRAMES = 50
REDACTED = "<redacted>"

OptExcInfo: TypeAlias = ExcInfo | tuple[None, None, None]


class _LocalsRedactor:
    """
    Decide which frame locals are redacted based on variable-name globs.

    Matching is case-insensitive and supports the same wildcard syntax as
    :mod:`fnmatch`.  The patterns are copied and lower-cased once at
    construction time, so later mutations of the caller's sequence do not
    affect the running redaction policy.
    """

    def __init__(self, patterns: Iterable[str]) -> None:
        self._patterns = tuple(str(pattern).lower() for pattern in patterns)

    @property
    def patterns(self) -> tuple[str, ...]:
        """Frozen, normalized copy of the glob patterns."""
        return self._patterns

    def is_redacted(self, name: str) -> bool:
        lowered = name.lower()
        return any(
            fnmatch.fnmatchcase(lowered, pattern) for pattern in self._patterns
        )


@dataclass
class Frame:
    """
    Represents a single stack frame.
    """

    filename: str
    lineno: int
    name: str
    locals: dict[str, str] | None = None


@dataclass
class SyntaxError_:  # noqa: N801
    """
    Contains detailed information about :exc:`SyntaxError` exceptions.
    """

    offset: int
    filename: str
    line: str
    lineno: int
    msg: str


@dataclass
class Stack:
    """
    Represents an exception and a list of stack frames.

    .. versionchanged:: 25.2.0
       Added the *exc_notes* field.

    .. versionchanged:: 25.4.0
       Added the *is_group* and *exceptions* fields.
    """

    exc_type: str
    exc_value: str
    exc_notes: list[str] = field(default_factory=list)
    syntax_error: SyntaxError_ | None = None
    is_cause: bool = False
    frames: list[Frame] = field(default_factory=list)
    is_group: bool = False
    exceptions: list[Trace] = field(default_factory=list)


@dataclass
class Trace:
    """
    Container for a list of stack traces.
    """

    stacks: list[Stack]


def safe_str(_object: Any) -> str:
    """Don't allow exceptions from __str__ to propagate."""
    try:
        return str(_object)
    except Exception as error:  # noqa: BLE001
        return f"<str-error {str(error)!r}>"


def to_repr(
    obj: Any,
    max_length: int | None = None,
    max_string: int | None = None,
    use_rich: bool = True,
) -> str:
    """
    Get repr string for an object, but catch errors.

    :func:`repr()` is used for strings, too, so that secret wrappers that
    inherit from :func:`str` and overwrite ``__repr__()`` are handled correctly
    (i.e. secrets are not logged in plain text).

    Args:
        obj: Object to get a string representation for.

        max_length: Maximum length of containers before abbreviating, or
            ``None`` for no abbreviation.

        max_string: Maximum length of string before truncating, or ``None`` to
            disable truncating.

        use_rich: If ``True`` (the default), use rich_ to compute the repr.
            If ``False`` or if rich_ is not installed, fall back to a simpler
            algorithm.

    Returns:
        The string representation of *obj*.

    .. versionchanged:: 24.3.0
       Added *max_length* argument.  Use :program:`rich` to render locals if it
       is available.  Call :func:`repr()` on strings in fallback
       implementation.
    """
    if use_rich and rich is not None:
        # Let rich render the repr if it is available.
        # It produces much better results for containers and dataclasses/attrs.
        obj_repr = rich.pretty.traverse(
            obj, max_length=max_length, max_string=max_string
        ).render()
    else:
        # Generate a (truncated) repr if rich is not available.
        # Handle str/bytes differently to get better results for truncated
        # representations.  Also catch all errors, similarly to "safe_str()".
        try:
            if isinstance(obj, (str, bytes)):
                if max_string is not None and len(obj) > max_string:
                    truncated = len(obj) - max_string
                    obj_repr = f"{obj[:max_string]!r}+{truncated}"
                else:
                    obj_repr = repr(obj)
            else:
                obj_repr = repr(obj)
                if max_string is not None and len(obj_repr) > max_string:
                    truncated = len(obj_repr) - max_string
                    obj_repr = f"{obj_repr[:max_string]!r}+{truncated}"
        except Exception as error:  # noqa: BLE001
            obj_repr = f"<repr-error {str(error)!r}>"

    return obj_repr


def _iter_visible_locals(
    iter_locals: Iterable[tuple[str, object]],
    redactor: _LocalsRedactor,
    locals_hide_dunder: bool,
    locals_hide_sunder: bool,
) -> Iterable[tuple[str, object]]:
    """Yield locals that are not hidden by the dunder/sunder rules."""
    if not (locals_hide_dunder or locals_hide_sunder):
        yield from iter_locals
        return
    for key, value in iter_locals:
        if redactor.is_redacted(key):
            # Redaction takes precedence over dunder/sunder hiding: a matched
            # variable keeps its name, only the value is replaced.
            yield key, value
            continue
        if locals_hide_dunder and key.startswith("__"):
            continue
        if locals_hide_sunder and key.startswith("_"):
            continue
        yield key, value


def _render_locals(
    frame_locals: dict[str, object],
    *,
    locals_max_length: int | None,
    locals_max_string: int | None,
    use_rich: bool,
    locals_hide_dunder: bool,
    locals_hide_sunder: bool,
    redactor: _LocalsRedactor,
) -> dict[str, str]:
    """Render frame locals, replacing matched names without touching them."""
    return {
        key: (
            REDACTED
            if redactor.is_redacted(key)
            else to_repr(
                value,
                max_length=locals_max_length,
                max_string=locals_max_string,
                use_rich=use_rich,
            )
        )
        for key, value in _iter_visible_locals(
            frame_locals.items(),
            redactor,
            locals_hide_dunder,
            locals_hide_sunder,
        )
    }


def extract(
    exc_type: type[BaseException],
    exc_value: BaseException,
    traceback: TracebackType | None,
    *,
    show_locals: bool = False,
    locals_max_length: int = LOCALS_MAX_LENGTH,
    locals_max_string: int = LOCALS_MAX_STRING,
    locals_hide_dunder: bool = True,
    locals_hide_sunder: bool = False,
    use_rich: bool = True,
    redact_locals: Iterable[str] = (),
    _seen: set[int] | None = None,
    _suppress: Sequence[str] = (),
) -> Trace:
    """
    Extract traceback information.

    Args:
        exc_type: Exception type.

        exc_value: Exception value.

        traceback: Python Traceback object.

        show_locals: Enable display of local variables. Defaults to False.

        locals_max_length:
            Maximum length of containers before abbreviating, or ``None`` for
            no abbreviation.

        locals_max_string:
            Maximum length of string before truncating, or ``None`` to disable
            truncating.

        locals_hide_dunder:
            Hide locals prefixed with double underscore.
            Defaults to True.

        locals_hide_sunder:
            Hide locals prefixed with single underscore.
            This implies hiding *locals_hide_dunder*.
            Defaults to False.

        use_rich: If ``True`` (the default), use rich_ to compute the repr.
            If ``False`` or if rich_ is not installed, fall back to a simpler
            algorithm.

        redact_locals:
            Optional iterable of case-insensitive :mod:`fnmatch` glob
            patterns.  Local variables whose name matches one of the patterns
            keep their name but get the fixed value ``"<redacted>"``.  The
            value of such a variable is never converted to a string -- no
            ``repr()``, ``str()``, Rich pretty protocol or recursive
            traversal is invoked on it.  Variables that do not match are still
            subject to *locals_max_length*, *locals_max_string*,
            *locals_hide_dunder* and *locals_hide_sunder*.  The patterns are
            copied, so mutating the passed-in iterable afterwards does not
            change the behavior of this call.

    Returns:
        A Trace instance with structured information about all exceptions.

    .. versionadded:: 22.1.0

    .. versionchanged:: 24.3.0
       Added *locals_max_length*, *locals_hide_sunder*, *locals_hide_dunder*
       and *use_rich* arguments.

    .. versionchanged:: 25.4.0
       Handle exception groups.

    .. versionchanged:: 25.5.0
       Handle loops in exception cause chain.

    .. versionchanged:: 26.2.0
       Added *redact_locals* argument.
    """

    redactor = _LocalsRedactor(redact_locals)
    stacks: list[Stack] = []
    is_cause = False

    if _seen is None:
        _seen = set()

    while True:
        exc_id = id(exc_value)
        if exc_id in _seen:
            break
        _seen.add(exc_id)

        stack = Stack(
            exc_type=safe_str(exc_type.__name__),
            exc_value=safe_str(exc_value),
            exc_notes=[
                safe_str(note) for note in getattr(exc_value, "__notes__", ())
            ],
            is_cause=is_cause,
        )

        if sys.version_info >= (3, 11):
            if isinstance(exc_value, (BaseExceptionGroup, ExceptionGroup)):  # noqa: F821
                stack.is_group = True
                for exception in exc_value.exceptions:
                    stack.exceptions.append(
                        extract(
                            type(exception),
                            exception,
                            exception.__traceback__,
                            show_locals=show_locals,
                            locals_max_length=locals_max_length,
                            locals_max_string=locals_max_string,
                            locals_hide_dunder=locals_hide_dunder,
                            locals_hide_sunder=locals_hide_sunder,
                            use_rich=use_rich,
                            redact_locals=redactor.patterns,
                            _seen=_seen,
                            _suppress=_suppress,
                        )
                    )

        if isinstance(exc_value, SyntaxError):
            stack.syntax_error = SyntaxError_(
                offset=exc_value.offset or 0,
                filename=exc_value.filename or "?",
                lineno=exc_value.lineno or 0,
                line=exc_value.text or "",
                msg=exc_value.msg,
            )

        stacks.append(stack)
        append = stack.frames.append

        for frame_summary, line_no in walk_tb(traceback):
            filename = frame_summary.f_code.co_filename
            if filename and not filename.startswith("<"):
                filename = os.path.abspath(filename)
            # Rich has this, but we are not rich and like to keep all frames:
            # if frame_summary.f_locals.get("_rich_traceback_omit", False):
            #     continue  # noqa: ERA001

            # Frames below a suppressed path must not expose their locals at
            # all: don't even look at their values, so __repr__/__str__ and
            # Rich's traversal can never be triggered on them.
            frame_is_suppressed = bool(_suppress) and any(
                (filename or "?").startswith(path) for path in _suppress
            )

            frame = Frame(
                filename=filename or "?",
                lineno=line_no,
                name=frame_summary.f_code.co_name,
                locals=(
                    _render_locals(
                        frame_summary.f_locals,
                        locals_max_length=locals_max_length,
                        locals_max_string=locals_max_string,
                        use_rich=use_rich,
                        locals_hide_dunder=locals_hide_dunder,
                        locals_hide_sunder=locals_hide_sunder,
                        redactor=redactor,
                    )
                    if show_locals and not frame_is_suppressed
                    else None
                ),
            )
            append(frame)

        cause = getattr(exc_value, "__cause__", None)
        if cause and cause.__traceback__:
            exc_type = cause.__class__
            exc_value = cause
            traceback = cause.__traceback__
            is_cause = True
            continue

        cause = exc_value.__context__
        if (
            cause
            and cause.__traceback__
            and not getattr(exc_value, "__suppress_context__", False)
        ):
            exc_type = cause.__class__
            exc_value = cause
            traceback = cause.__traceback__
            is_cause = False
            continue

        # No cover, code is reached but coverage doesn't recognize it.
        break  # pragma: no cover

    return Trace(stacks=stacks)


class ExceptionDictTransformer:
    """
    Return a list of exception stack dictionaries for an exception.

    These dictionaries are based on :class:`Stack` instances generated by
    :func:`extract()` and can be dumped to JSON.

    Args:
        show_locals:
            Whether or not to include the values of a stack frame's local
            variables.

        locals_max_length:
            Maximum length of containers before abbreviating, or ``None`` for
            no abbreviation.

        locals_max_string:
            Maximum length of string before truncating, or ``None`` to disable
            truncating.

        locals_hide_dunder:
            Hide locals prefixed with double underscore.
            Defaults to True.

        locals_hide_sunder:
            Hide locals prefixed with single underscore.
            This implies hiding *locals_hide_dunder*.
            Defaults to False.

        suppress:
            Optional sequence of modules or paths for which to suppress the
            display of locals even if *show_locals* is ``True``.

        max_frames:
            Maximum number of frames in each stack.  Frames are removed from
            the inside out.  The idea is, that the first frames represent your
            code responsible for the exception and last frames the code where
            the exception actually happened.  With larger web frameworks, this
            does not always work, so you should stick with the default.

        use_rich: If ``True`` (the default), use rich_ to compute the repr of
            locals.  If ``False`` or if rich_ is not installed, fall back to
            a simpler algorithm.

        redact_locals:
            Optional iterable of case-insensitive :mod:`fnmatch` glob
            patterns (for example ``("password", "*token*")``).  Local
            variables whose name matches one of the patterns are still listed
            by their name, but their value is replaced with the fixed string
            ``"<redacted>"``.  A redacted value is never stringified: neither
            ``repr()``/``str()`` nor Rich's pretty protocol is called on it
            and its contents are never traversed recursively.  Variables that
            do not match keep being subject to *locals_max_length*,
            *locals_max_string*, *locals_hide_dunder* and
            *locals_hide_sunder*.  The patterns are copied into a frozen
            tuple at construction time; later changes to the caller's
            sequence do not affect this transformer.

            Only local variables selected by name are protected.  Sensitive
            text that is part of an exception message itself is not detected
            or redacted automatically.

    .. seealso::
        :doc:`exceptions` for a broader explanation of *structlog*'s exception
        features.

    .. versionchanged:: 24.3.0
       Added *locals_max_length*, *locals_hide_sunder*, *locals_hide_dunder*,
       *suppress* and *use_rich* arguments.

    .. versionchanged:: 25.1.0
       *locals_max_length* and *locals_max_string* may be None to disable
       truncation.

    .. versionchanged:: 25.4.0
       Handle exception groups.

    .. versionchanged:: 26.2.0
       Added *redact_locals* argument.
    """

    show_locals: bool
    locals_max_length: int
    locals_max_string: int
    locals_hide_dunder: bool
    locals_hide_sunder: bool
    suppress: Sequence[str]
    max_frames: int
    use_rich: bool
    redact_locals: tuple[str, ...]

    def __init__(
        self,
        *,
        show_locals: bool = SHOW_LOCALS,
        locals_max_length: int = LOCALS_MAX_LENGTH,
        locals_max_string: int = LOCALS_MAX_STRING,
        locals_hide_dunder: bool = True,
        locals_hide_sunder: bool = False,
        suppress: Iterable[str | ModuleType] = (),
        max_frames: int = MAX_FRAMES,
        use_rich: bool = True,
        redact_locals: Iterable[str] = (),
    ) -> None:
        if locals_max_length is not None and locals_max_length < 0:
            msg = f'"locals_max_length" must be >= 0: {locals_max_length}'
            raise ValueError(msg)
        if locals_max_string is not None and locals_max_string < 0:
            msg = f'"locals_max_string" must be >= 0: {locals_max_string}'
            raise ValueError(msg)
        if max_frames < 2:
            msg = f'"max_frames" must be >= 2: {max_frames}'
            raise ValueError(msg)
        self.show_locals = show_locals
        self.locals_max_length = locals_max_length
        self.locals_max_string = locals_max_string
        self.locals_hide_dunder = locals_hide_dunder
        self.locals_hide_sunder = locals_hide_sunder
        self.suppress = []
        for suppress_entity in suppress:
            if not isinstance(suppress_entity, str):
                if suppress_entity.__file__ is None:
                    msg = (
                        f'"suppress" item {suppress_entity!r} must be a '
                        f"module with '__file__' attribute"
                    )
                    raise ValueError(msg)
                path = os.path.dirname(suppress_entity.__file__)
            else:
                path = suppress_entity
            path = os.path.normpath(os.path.abspath(path))
            self.suppress.append(path)
        self.max_frames = max_frames
        self.use_rich = use_rich
        # Frozen, case-normalized private copy: later mutations of the
        # caller's iterable must not change the redaction policy in effect.
        self.redact_locals = tuple(
            str(pattern).lower() for pattern in redact_locals
        )

    def __call__(self, exc_info: ExcInfo) -> list[dict[str, Any]]:
        trace = extract(
            *exc_info,
            show_locals=self.show_locals,
            locals_max_length=self.locals_max_length,
            locals_max_string=self.locals_max_string,
            locals_hide_dunder=self.locals_hide_dunder,
            locals_hide_sunder=self.locals_hide_sunder,
            use_rich=self.use_rich,
            redact_locals=self.redact_locals,
            _suppress=self.suppress,
        )

        for stack in trace.stacks:
            if len(stack.frames) <= self.max_frames:
                continue

            half = (
                self.max_frames // 2
            )  # Force int division to handle odd numbers correctly
            fake_frame = Frame(
                filename="",
                lineno=-1,
                name=f"Skipped frames: {len(stack.frames) - (2 * half)}",
            )
            stack.frames[:] = [
                *stack.frames[:half],
                fake_frame,
                *stack.frames[-half:],
            ]

        return self._as_dict(trace)

    def _as_dict(self, trace: Trace) -> list[dict[str, Any]]:
        stack_dicts = []
        for stack in trace.stacks:
            stack_dict = asdict(stack)
            for frame_dict in stack_dict["frames"]:
                if frame_dict["locals"] is None or any(
                    frame_dict["filename"].startswith(path)
                    for path in self.suppress
                ):
                    del frame_dict["locals"]
            if stack.is_group:
                stack_dict["exceptions"] = [
                    self._as_dict(t) for t in stack.exceptions
                ]
            stack_dicts.append(stack_dict)
        return stack_dicts
