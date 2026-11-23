"""Line-level Beancount reading and rendering for the account-facing subset.

The domain owns only ``open`` metadata, ``balance`` assertions, ``close``
directives, ``include`` lines and one rendered draft transaction. Every other
byte stays where it was. Full validation belongs to ``bean-check``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .amounts import assert_amount, normalize_amount
from .errors import fail

ACCOUNT_RE = re.compile(r"^[A-Z][A-Za-z0-9-]*(?::[A-Z][A-Za-z0-9-]*)+$")
CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9'._-]{0,21}[A-Z0-9]$")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-/.]{0,63}$")
DATE_TEXT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
METADATA_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
FLAG_RE = re.compile(r"^[*!&#?%PSTCURM]$")
METADATA_RE = re.compile(r"^[ \t]+([A-Za-z][A-Za-z0-9_-]*)[ \t]*:[ \t]*(.*)$")
OPEN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ \t]+open[ \t]+(\S+)(.*)$")
BALANCE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ \t]+balance[ \t]+(\S+)[ \t]+([-+]?\d+(?:\.\d+)?)[ \t]+(\S+)$"
)
CLOSE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ \t]+close[ \t]+(\S+)$")
INCLUDE_RE = re.compile(r'^include[ \t]+"([^"]+)"$')
POSTING_ACCOUNT_RE = re.compile(r"^[ \t]+([A-Z][A-Za-z0-9-]*(?::[A-Z][A-Za-z0-9-]*)+)(?:[ \t]|$)")


@dataclass
class Document:
    """A file split into lines while remembering its newline style."""

    lines: list[str]
    eol: str
    final_newline: bool

    def render(self) -> str:
        return self.eol.join(self.lines)


def is_account_name(value: object) -> bool:
    return isinstance(value, str) and ACCOUNT_RE.match(value) is not None


def is_currency(value: object) -> bool:
    return isinstance(value, str) and CURRENCY_RE.match(value) is not None


def assert_alias(value: object) -> str:
    """Validate one display alias, which must never shadow a canonical account."""
    if not isinstance(value, str):
        fail("invalid_alias", "alias must be a string")
    alias = value.strip()
    if not alias:
        fail("invalid_alias", "alias must not be empty")
    if len(alias) > 40:
        fail("invalid_alias", "alias must be at most 40 characters")
    if re.search(r"[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]", alias):
        fail("invalid_alias", "alias must not contain control or bidirectional-override characters")
    if '"' in alias or "\\" in alias:
        fail("invalid_alias", "alias must not contain a double quote or backslash")
    if is_account_name(alias):
        fail("invalid_alias", f'"{alias}" is a canonical account name; an alias cannot shadow one')
    return alias


def normalize_alias(alias: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", alias).split()).lower()


def split_document(content: str) -> Document:
    """Split a file into lines and remember whether it uses CRLF."""
    eol = "\r\n" if "\r\n" in content else "\n"
    normalized = content.replace("\r\n", "\n") if eol == "\r\n" else content
    return Document(normalized.split("\n"), eol, content.endswith("\n"))


def strip_comment(line: str) -> str:
    """Remove a trailing ``;`` comment without touching quoted semicolons."""
    in_string = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_string and character == "\\":
            escaped = True
            continue
        if character == '"':
            in_string = not in_string
            continue
        if character == ";" and not in_string:
            return line[:index]
    return line


def parse_open_line(line: str) -> dict[str, Any] | None:
    match = OPEN_RE.match(line)
    if match is None or not is_account_name(match.group(2)):
        return None
    rest = strip_comment(match.group(3)).strip()
    currencies = [part for part in re.split(r"[,\s]+", rest) if part] if rest else []
    if any(not is_currency(currency) for currency in currencies):
        return None
    return {"date": match.group(1), "account": match.group(2), "currencies": currencies}


def parse_balance_line(line: str) -> dict[str, Any] | None:
    match = BALANCE_RE.match(strip_comment(line).strip())
    if match is None or not is_account_name(match.group(2)) or not is_currency(match.group(4)):
        return None
    return {"date": match.group(1), "account": match.group(2), "amount": match.group(3), "currency": match.group(4)}


def parse_close_line(line: str) -> dict[str, Any] | None:
    match = CLOSE_RE.match(strip_comment(line).strip())
    if match is None or not is_account_name(match.group(2)):
        return None
    return {"date": match.group(1), "account": match.group(2)}


def parse_include_line(line: str) -> str | None:
    match = INCLUDE_RE.match(strip_comment(line).strip())
    return match.group(1) if match else None


def parse_metadata_line(line: str) -> dict[str, str] | None:
    match = METADATA_RE.match(line)
    return {"key": match.group(1), "raw": match.group(2)} if match else None


def parse_quoted_string(raw: str) -> str | None:
    match = re.match(r'^"((?:[^"\\]|\\.)*)"', raw.strip())
    if match is None:
        return None
    return re.sub(r'\\(["\\])', r"\1", match.group(1))


def parse_posting_account(line: str) -> str | None:
    """Read an indented posting's account; metadata keys start lowercase and never match."""
    match = POSTING_ACCOUNT_RE.match(line)
    return match.group(1) if match else None


def read_metadata_block(lines: list[str], start: int) -> tuple[int, list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            break
        if not line[:1].isspace():
            break
        metadata = parse_metadata_line(line)
        if metadata is not None:
            entries.append({**metadata, "line_index": index})
        index += 1
    return index, entries


def find_open_directive(lines: list[str], account: str) -> dict[str, Any] | None:
    for index, line in enumerate(lines):
        opened = parse_open_line(line)
        if opened is None or opened["account"] != account:
            continue
        end, entries = read_metadata_block(lines, index + 1)
        return {
            "line_index": index,
            "block_start": index + 1,
            "block_end": end,
            "entries": entries,
            "date": opened["date"],
            "currencies": opened["currencies"],
        }
    return None


def metadata_value(location: dict[str, Any], key: str) -> str | None:
    for entry in location["entries"]:
        if entry["key"] == key:
            return parse_quoted_string(entry["raw"])
    return None


def quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def rewrite_open_metadata(content: str, account: str, values: dict[str, str | None]) -> tuple[str, bool]:
    """Rewrite only alias/reconcile metadata on one open directive.

    ``None`` removes a key; an absent key is left untouched. Everything else in
    the block (comments, other metadata, indentation) is preserved.
    """
    document = split_document(content)
    lines = document.lines
    location = find_open_directive(lines, account)
    if location is None:
        fail("account_not_found", f"no open directive for {account} in this file")

    set_values: dict[str, str] = {}
    unset_values: set[str] = set()
    for key in ("alias", "reconcile"):
        if key not in values:
            continue
        value = values[key]
        if value is None:
            unset_values.add(key)
        elif isinstance(value, str):
            set_values[key] = value
        else:
            fail("invalid_argument", f"{key} must be a string or null")

    kept: list[str] = []
    seen: set[str] = set()
    for index in range(location["block_start"], location["block_end"]):
        line = lines[index]
        metadata = parse_metadata_line(line)
        if metadata is None:
            kept.append(line)
            continue
        key = metadata["key"]
        if key in unset_values:
            continue
        if key in set_values:
            if key in seen:
                continue
            seen.add(key)
            indent = re.match(r"^[ \t]*", line).group(0)
            kept.append(f"{indent}{key}: {quote(set_values[key])}")
            continue
        kept.append(line)
    for key, value in set_values.items():
        if key not in seen:
            kept.append(f"  {key}: {quote(value)}")

    next_lines = lines[: location["block_start"]] + kept + lines[location["block_end"] :]
    next_content = document.eol.join(next_lines)
    return next_content, next_content != content


def rewrite_open_date(content: str, account: str, date: str) -> tuple[str, bool]:
    """Move one open directive to a new date, preserving the rest of the line.

    Only the leading ``YYYY-MM-DD`` token changes; currencies, a trailing
    comment, the metadata block and indentation are left exactly as they were.
    """
    if not DATE_TEXT_RE.match(date):
        fail("invalid_date", "open date must be YYYY-MM-DD")
    document = split_document(content)
    lines = document.lines
    location = find_open_directive(lines, account)
    if location is None:
        fail("account_not_found", f"no open directive for {account} in this file")
    index = location["line_index"]
    match = re.match(r"^([ \t]*)(\d{4}-\d{2}-\d{2})", lines[index])
    if match is None:
        fail("invalid_account", f"cannot locate the date token of the {account} open directive")
    next_lines = list(lines)
    next_lines[index] = f"{match.group(1)}{date}{lines[index][match.end():]}"
    next_content = document.eol.join(next_lines)
    return next_content, next_content != content


def render_balance(assertion: dict[str, Any]) -> str:
    date = assertion["date"]
    if not DATE_TEXT_RE.match(date):
        fail("invalid_date", "balance date must be YYYY-MM-DD")
    if not is_account_name(assertion["account"]):
        fail("invalid_account", f'"{assertion["account"]}" is not a Beancount account')
    if not is_currency(assertion["currency"]):
        fail("invalid_currency", f'"{assertion["currency"]}" is not a Beancount currency')
    assert_amount(assertion["amount"])
    return f"{date} balance {assertion['account']} {normalize_amount(assertion['amount'])} {assertion['currency']}"


def render_transaction(transaction: dict[str, Any]) -> str:
    """Render one transaction from canonical, explicit postings."""
    if not DATE_TEXT_RE.match(transaction["date"]):
        fail("invalid_date", "transaction date must be YYYY-MM-DD")
    flag = transaction.get("flag") or "*"
    if not FLAG_RE.match(flag):
        fail("invalid_flag", f'"{flag}" is not a Beancount transaction flag')
    tags = transaction.get("tags") or []
    links = transaction.get("links") or []
    for tag in tags:
        if not TAG_RE.match(tag):
            fail("invalid_tag", f'"{tag}" is not a Beancount tag')
    for link in links:
        if not TAG_RE.match(link):
            fail("invalid_link", f'"{link}" is not a Beancount link')
    suffix = " ".join([*(f"#{tag}" for tag in tags), *(f"^{link}" for link in links)])
    header = (
        f"{transaction['date']} {flag} {quote(transaction.get('payee') or '')} "
        f"{quote(transaction.get('narration') or '')}{' ' + suffix if suffix else ''}"
    )
    lines = [header]
    metadata = transaction.get("metadata")
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if not isinstance(key, str) or METADATA_KEY_RE.match(key) is None:
                fail("invalid_metadata", f'"{key}" is not a valid Beancount metadata key')
            if key == "draft_id":
                fail("invalid_metadata", "draft_id is reserved by the transaction renderer")
            lines.append(f"  {key}: {quote(str(value))}")
    draft_id = transaction.get("draft_id")
    if isinstance(draft_id, str) and draft_id:
        lines.append(f"  draft_id: {quote(draft_id)}")
    postings = transaction.get("postings") or []
    if len(postings) < 2:
        fail("invalid_draft", "a transaction needs at least two postings")
    for posting in postings:
        if not is_account_name(posting["account"]):
            fail("invalid_account", f'"{posting["account"]}" is not a Beancount account')
        if not is_currency(posting["currency"]):
            fail("invalid_currency", f'"{posting["currency"]}" is not a Beancount currency')
        lines.append(f"  {posting['account']}  {normalize_amount(posting['amount'])} {posting['currency']}")
    return "\n".join(lines)


def append_block(content: str, block: str) -> str:
    if content == "":
        return block + "\n"
    base = content if content.endswith("\n") else content + "\n"
    separator = "" if re.search(r"\n[ \t]*\n$", base) else "\n"
    return f"{base}{separator}{block}\n"


def contains_draft_id(content: str, draft_id: str) -> bool:
    escaped = draft_id.replace('"', '\\"')
    return f'draft_id: "{escaped}"' in content


def next_line_number(content: str) -> int:
    if content == "":
        return 1
    document = split_document(content)
    return len(document.lines) if content.endswith("\n") else len(document.lines) + 1


def render_open(account: dict[str, Any]) -> str:
    """Render one open directive with optional alias/reconcile metadata."""
    date = account["date"]
    name = account["account"]
    if not DATE_TEXT_RE.match(date):
        fail("invalid_date", "open date must be YYYY-MM-DD")
    if not is_account_name(name):
        fail("invalid_account", f'"{name}" is not a Beancount account')
    currencies = account.get("currencies") or []
    for currency in currencies:
        if not is_currency(currency):
            fail("invalid_currency", f'"{currency}" is not a Beancount currency')
    line = f"{date} open {name}"
    if currencies:
        line += " " + ",".join(currencies)
    lines = [line]
    alias = account.get("alias")
    if alias is not None:
        lines.append(f"  alias: {quote(alias)}")
    reconcile = account.get("reconcile")
    if reconcile is not None:
        lines.append(f"  reconcile: {quote(reconcile)}")
    return "\n".join(lines)
