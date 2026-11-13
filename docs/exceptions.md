# Exceptions

While you should use a proper crash reporter like [Sentry](https://sentry.io) in production, *structlog* has helpers for formatting exceptions for humans and machines.

All *structlog*'s exception features center around passing an `exc_info` key-value pair in the event dict.
There are three possible behaviors depending on its value:

1. If the value is a tuple, render it as if it was returned by {func}`sys.exc_info`.
2. If the value is an Exception, render it.
3. If the value is true but no tuple, call {func}`sys.exc_info` and render that.

If there is no `exc_info` key or false, the event dict is not touched.
This behavior is analog to the one of the stdlib's logging.


## Transformations

*structlog* comes with {class}`structlog.processors.ExceptionRenderer` that deduces and removes the `exc_info` key as outlined above, calls a user-supplied function with the synthesized `exc_info`, and stores its return value in the `exception` key.
The most common use-cases are already covered by the following processors:

{func}`structlog.processors.format_exc_info`

: Formats it to a flat string like the standard library would on the console.

{obj}`structlog.processors.dict_tracebacks`

: Uses {class}`structlog.tracebacks.ExceptionDictTransformer` to give you a structured and JSON-serializable `exception` key.


### Customizing Dict Tracebacks

{class}`structlog.tracebacks.ExceptionDictTransformer` can be customized with a number of parameters.
If these prove insufficient, see {ref}`Custom Filtering of Dict Tracebacks <custom-dict-tracebacks>`.


### Redacting Local Variables

If you log local variables (the default for dict tracebacks), secrets like passwords or access tokens end up in your monitoring backend.
Filtering the rendered JSON afterwards is not enough: by then the value has already been passed through `repr()` -- or Rich's pretty printer, which recursively walks objects -- and some objects trigger network calls or raise when rendered.

`ExceptionDictTransformer` therefore takes a `redact_locals` argument with a list of case-insensitive [glob patterns](https://docs.python.org/3/library/fnmatch.html).
A matching local keeps its name, but its value is replaced with the fixed string `"<redacted>"` *before* any rendering happens, so neither `repr()`, `str()`, Rich's pretty protocol nor recursive traversal is ever invoked on it.
Variables that don't match a pattern continue to follow the regular `locals_max_length`, `locals_max_string` and dunder/sunder rules.
The patterns are copied into a frozen tuple when the transformer is constructed, so mutating your original list later does not affect logging.

The same rules apply to every exception in the trace: regular exceptions, explicit causes (`raise ... from ...`), implicit exception contexts and every branch of an `ExceptionGroup`.
Frames hidden through `suppress=` or rendered with `show_locals=False` are never inspected in the first place.

```python
import structlog
from structlog.processors import ExceptionRenderer, JSONRenderer
from structlog.tracebacks import ExceptionDictTransformer

structlog.configure(
    processors=[
        ExceptionRenderer(
            ExceptionDictTransformer(
                redact_locals=("password", "*token*", "*secret*"),
            )
        ),
        JSONRenderer(),
    ]
)

logger = structlog.get_logger()

try:
    ...
except Exception:
    # Matching locals appear as {"password": "<redacted>"} in the JSON.
    logger.exception("request failed")
```

Note that redaction only covers frame locals selected by variable name.
Sensitive text embedded in an exception *message* itself is not detected automatically.


## Console rendering

Our {doc}`console-output`'s {class}`structlog.dev.ConsoleRenderer` takes an *exception_formatter* argument that allows for customizing the output of exceptions.

{func}`structlog.dev.plain_traceback`

: Is the default if [Rich] is not installed.
  As the name suggests, it renders a plain traceback.

{class}`structlog.dev.RichTracebackFormatter`

: Uses [Rich] to render a colorful traceback.
  It's a class because it allows for customizing the output by passing arguments to Rich.
: It's the default if Rich is installed.

:::{seealso}
{doc}`console-output` for more information on *structlog*'s console features.
:::

:::{deprecated} 26.1.0
Support for [*better-exceptions*] is deprecated and will be removed in a future release.
Use [Rich] instead.
:::

[*better-exceptions*]: https://github.com/qix-/better-exceptions
[Rich]: https://github.com/Textualize/rich
