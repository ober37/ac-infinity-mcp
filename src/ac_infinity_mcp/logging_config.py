import logging
import re

# Credential markers redacted in formatted log output. Tuple of (field_name,
# value_pattern) — the field-name alternation matches the marker token, and the
# value pattern is intentionally permissive to handle:
#   field=value        (positional log args, e.g. "token=abc123")
#   'field': 'value'   (dict repr from logger.debug("%s", payload_dict))
#   "field": "value"   (json.dumps output)
#   field=value with spaces in the value (greedy until a structural terminator)
#   url?userId=value   (query-string credentials in HTTPError __str__)
_FIELD_PATTERN = re.compile(
    r"(appPasswordl|appPassword|AC_INFINITY_PASSWORD|appEmail|token|appId|userId)"
    r"(['\"]?\s*[:=]\s*)"
    # Value: either quoted (any chars until matching quote) or unquoted (any
    # chars until a structural terminator). The terminator set covers JSON
    # delimiters (newline, comma, closing brace/bracket) AND URL/query
    # separators (`&`, `;`) so URL-query credentials don't swallow trailing
    # params — `?userId=tok&other=val` redacts only the token, not `&other=val`.
    #
    # A naked whitespace inside a value (e.g. password with embedded space) is
    # preserved as part of the value so we never under-redact. The remaining
    # edge case — two adjacent credential markers in space-separated positional
    # form on the same log line — does not occur in any production log site in
    # this server and is documented as an accepted trade-off.
    r"(?:(['\"])([^'\"]*)\3|([^\n,}\];&]+))",
    re.IGNORECASE,
)


def _redact_credentials(text: str) -> str:
    """Redact credential-field values from any text. Idempotent."""
    if not text:
        return text

    def _sub(match: re.Match[str]) -> str:
        field = match.group(1)
        sep = match.group(2)
        quote = match.group(3)
        if quote is not None:
            return f"{field}{sep}{quote}<redacted>{quote}"
        return f"{field}{sep}<redacted>"

    return _FIELD_PATTERN.sub(_sub, text)


class _CredentialRedactingFormatter(logging.Formatter):
    """Formatter that scrubs credential markers from both the message line AND
    any exception text (the traceback emitted by ``exc_info=True``).

    Uses a Formatter subclass rather than a logging.Filter so that exception
    tracebacks — emitted after format() by every ``logger.error(...,
    exc_info=True)`` site — are also scrubbed. Defense in depth: every
    existing log call site is audited clean; the formatter prevents future leaks.
    """

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return _redact_credentials(formatted)

    def formatException(self, ei: object) -> str:  # type: ignore[override]
        return _redact_credentials(super().formatException(ei))  # type: ignore[arg-type]


def _install_credential_redactor(target_logger: logging.Logger | None = None) -> None:
    """Attach the credential-redacting formatter to every handler on the root logger.

    Filters on logger objects (vs handlers) skip records propagated up from
    child loggers — Python's logging design. Attaching at the handler layer
    means every record emitted to a sink (stderr, file) passes through the
    redactor regardless of origin logger. Also called from tests after they
    add their own handlers.
    """
    target = target_logger or logging.getLogger()
    for handler in target.handlers:
        # Preserve any existing format string the operator may have configured.
        existing_fmt = handler.formatter._fmt if handler.formatter else None  # type: ignore[union-attr]
        handler.setFormatter(_CredentialRedactingFormatter(existing_fmt))
