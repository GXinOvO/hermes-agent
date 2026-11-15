"""Command-line flag and connection-string key masking for :mod:`agent.redact`."""
import re


# Command-line credentials. ``--password=…``, ``--token …`` and ``docker login -p …`` reach
# the approval card and the chat egress verbatim because the ENV passes only look at
# ``NAME=value`` (issue #143982). The flag name is kept so the reader still knows what was
# passed; only the value is masked. ``-p`` is scoped to a container ``login`` because
# everywhere else it is a port or a path.
# Each quote style is matched on its own, so ``"Abc'def ghi"`` is consumed whole instead of
# falling back to ``\S+`` at the apostrophe and leaving the tail visible. A double-quoted
# value honours backslash escapes (``"a\"b c"``) and a single-quoted one the shell's
# ``'\''`` splice, and either may span a newline. The body is capped and possessive: an
# unterminated or over-long quote masks through the end of its line instead of only its
# first word, and never rescans the rest of the text from every flag.
_QUOTED_FLAG_VALUE = (r"\"(?:\\[\s\S]|[^\"\\]){1,512}+\"|'(?:'\\''|[^']){1,512}+'"
                      r"|[\"'][^\n]*+|\S+")
_CLI_SECRET_FLAG_RE = re.compile(
    r"(?P<flag>(?<![\w-])--?(?:password|passwd|passphrase|pass|token|api[-_]?key|apikey"
    r"|client[-_]?secret|access[-_]?key|account[-_]?key|auth[-_]?token|session[-_]?token|secret|bearer"
    r"|credential)s?)"
    r"(?P<sep>\s*=|\s+)"
    rf"(?P<value>{_QUOTED_FLAG_VALUE})",
    re.IGNORECASE,
)
_CLI_LOGIN_P_RE = re.compile(
    r"(?P<flag>\b(?:docker|podman|nerdctl)\s+login\b[^\n;|&]*?\s-p\s*)"
    rf"(?P<value>{_QUOTED_FLAG_VALUE})"
)
# Azure connection-string keys. They are ``Name=value`` inside a quoted string, so
# every ENV pass declines them: the name is mixed case and carries no underscore boundary.
# The key must open a connection-string segment (start, ``;``, ``,``, a URL ``?``/``&``, a quote or whitespace before
# it, no space around ``=``): ``this.accountKey = x`` / ``dt.PrimaryKey = new …`` in source
# read through read_file or a terminal stays byte-identical, or the agent cannot patch it.
# A spaced ``AccountKey = …`` (ini files, PowerShell) only counts when the value is a
# 16+ char base64 run carrying a digit, which an identifier like ``config.Key`` or
# ``StorageAccountKeyFromVault`` is not.
_CONNSTR_SECRET_KEY_RE = re.compile(
    r"(?P<flag>(?<![^;,?&'\"\s])(?:AccountKey|SharedAccessKey|(?P<sas>SharedAccessSignature))"
    r"(?:=|[ \t]*+=[ \t]*+(?=[A-Za-z+/]*[0-9])(?=[A-Za-z0-9+/]{16,}={0,2}(?![\w.(]))))"
    # A SAS value is itself a query string (``sv=…&sig=…``): it runs through the ``sig=``
    # parameter and stops there, or at a ``;`` / ``)`` / ``,`` / ``|`` / backtick delimiter.
    r"(?P<value>(?(sas)(?:(?!sig=)[^;\s'\"&),`|]++&)*+(?:sig=)?[^;\s'\"&),`|]*+|[^;\s'\"&]+))",
    re.IGNORECASE,
)

# Cheap pre-gates: every flag the regexes above can match contains one of these.
_CLI_SECRET_FLAG_GATE = re.compile(r"-{1,2}[A-Za-z]")
_CLI_LOGIN_GATE = re.compile(r"(?:docker|podman|nerdctl)\s+login\b", re.IGNORECASE)
_CONNSTR_GATE = re.compile(r"(?:AccountKey|SharedAccessKey|SharedAccessSignature)", re.IGNORECASE)


def _mask_flag_value(match: "re.Match[str]", mask=None) -> str:
    """Mask the value of a flag/connection-string key, keeping the flag and any quotes.

    ``mask`` is the caller's mask function: ``file_read`` passes the non-reusable sentinel,
    so a file read never hands back a head/tail value shaped like a usable key (#35519)."""
    from agent.redact import _is_redaction_marker, _mask_token

    value = match.group("value")
    stripped = value.strip("'\"")
    if not stripped or _is_redaction_marker(match, stripped.strip()):
        return match.group(0)
    # ``--password=$DB_PASS`` / ``--token=${TOKEN}`` name a variable; the reference is
    # not the credential and masking it would make the command unreplayable.
    if stripped[0] in "$<{%":
        return match.group(0)
    if stripped.isdigit():
        return match.group(0)  # ``--token 60`` is a timeout, not a credential
    if match.re is _CLI_SECRET_FLAG_RE:
        if "=" not in match.group("sep") and _is_flag_placeholder(value):
            return match.group(0)
        if _is_flag_path_reference(match.group("flag"), stripped):
            return match.group(0)
    masked = (mask or _mask_token)(stripped)
    start = match.start("value") - match.start(0)
    end = match.end("value") - match.start(0)
    return match.group(0)[:start] + masked + match.group(0)[end:]


# A space-separated flag is followed by whatever comes next, which in ``--help`` output and
# prose is a placeholder or a word (``--token TOKEN``, ``[--password PASSWORD]``, ``use
# --token to pass…``), the next flag (``psql --password -h db``) or a sub-option spec
# (``docker build --secret id=npmrc``). None of those is the credential.
# A metavar is upper case (``TOKEN``, ``DB_PASSWORD``), two short snake-case words
# (``db_name``) or a one-hump camelCase type name (cobra's ``stringArray``); a prose word
# is short and lower case. A passphrase is longer than that: a long lower-case word
# (``correcthorsebatterystaple``), three or more words (``correct_horse_battery_staple``,
# ``correctHorseBatteryStaple``) or anything carrying a digit or symbol is still masked.
_FLAG_PLACEHOLDER_RE = re.compile(
    r"(?:[A-Z][A-Z_]*|[a-z]{1,8}|[a-z]{1,8}_[a-z]{1,8}|[a-z]{1,8}[A-Z][a-z]{1,8})[\]).,:;]*"
    r"|[-\[(<].*|[a-z][\w-]*=.*"
    r"|\S*(?:xxx|XXX|\.\.\.)"  # a docs sample: ``--api-key sk-or-v1-xxx``
)


def _is_flag_placeholder(value: str) -> bool:
    """True for an unquoted space-separated flag value that is a word, not a credential."""
    return value[0] not in "'\"" and _FLAG_PLACEHOLDER_RE.fullmatch(value) is not None


def _is_flag_path_reference(flag: str, value: str) -> bool:
    """``--credentials ~/.config/gcloud/creds.json`` names a file, same rule as the ENV passes:
    only password-class flags mask a path, and only while no segment looks opaque."""
    from agent.redact import _OPAQUE_PATH_SEGMENT_RE, _PASSWORD_KEY_RE

    if _PASSWORD_KEY_RE.search(flag) or not value.startswith(("/", "~", "./", "../")):
        return False
    return not any(_OPAQUE_PATH_SEGMENT_RE.fullmatch(seg) for seg in value.split("/"))


def _redact_cli_secrets(text: str, mask=None) -> str:
    """Mask credentials written as command-line flags and connection-string keys."""
    def sub(m: "re.Match[str]") -> str:
        return _mask_flag_value(m, mask)

    if "=" in text or " " in text:
        if _CLI_SECRET_FLAG_GATE.search(text):
            text = _CLI_SECRET_FLAG_RE.sub(sub, text)
        if _CLI_LOGIN_GATE.search(text):
            text = _CLI_LOGIN_P_RE.sub(sub, text)
    if _CONNSTR_GATE.search(text):
        text = _CONNSTR_SECRET_KEY_RE.sub(sub, text)
    return text
