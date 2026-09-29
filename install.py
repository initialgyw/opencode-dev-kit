#!/usr/bin/env python3
"""Install bundled and external OpenCode agents and skills with symlinks."""

from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import os
import posixpath
import re
import shutil
import stat
import sys
import unicodedata
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SCRIPT_DIR = Path(__file__).resolve().parent
BUNDLED_SENTINEL = "__bundled__"
VALID_AGENT_MODES = {"primary", "subagent", "all"}
PROFILE_MANAGED_AGENTS = frozenset({"coder", "researcher", "observer", "reviewer", "documenter"})
SAFE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILES = 512
MAX_COLLECTION_ITEMS = 256
MAX_CRAWL_DEPTH = 8
MAX_CRAWL_PAGES = 256
MAX_REDIRECTS = 5
HTTP_TIMEOUT_SECONDS = 30


class InstallError(RuntimeError):
    """An expected installation or source-resolution failure."""


@dataclass(frozen=True)
class SourceSpec:
    kind: str
    value: str
    origin: str
    bundled: bool = False


@dataclass
class InstallerConfig:
    skills: list[str]
    agents: list[str]
    plugins: list[str]
    providers: dict[str, dict[str, object]]
    model_aliases: dict[str, object]
    agent_models: dict[str, object]


@dataclass
class Artifact:
    kind: str
    name: str
    files: dict[str, bytes]
    origin: str
    bundled: bool
    link_source: Optional[Path] = None
    content_hash: str = ""
    cache_path: Optional[Path] = None
    agent_mode: Optional[str] = None

    def calculate_hash(self) -> str:
        digest = hashlib.sha256()
        for relative_path in sorted(self.files):
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(self.files[relative_path])
            digest.update(b"\0")
        return digest.hexdigest()

    @property
    def display_source(self) -> str:
        return "bundled" if self.bundled else redact_url(self.origin)


@dataclass
class ApplyOperation:
    destination: Path
    expected_target: Path
    backup: Optional[Path] = None
    original_symlink: Optional[str] = None
    original_symlink_is_directory: bool = False
    created_link: bool = False


@dataclass
class ConfigOperation:
    path: Path
    backup: Optional[Path]
    created: bool


@dataclass
class InstallPlan:
    artifacts: list[Artifact]
    target: Path
    backup_root: Path
    cache_root: Path
    state_path: Path
    runtime_config: Optional[dict[str, object]] = None
    provider_names: list[str] = field(default_factory=list)
    agent_routing: dict[str, dict[str, str]] = field(default_factory=dict)
    selected_profile: Optional[str] = None
    duplicate_messages: list[str] = field(default_factory=list)
    skipped_messages: list[str] = field(default_factory=list)


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


class RedirectLimitHandler(HTTPRedirectHandler):
    """Bound redirects so a source cannot create an unbounded fetch chain."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def redirect_request(self, request, fp, code, message, headers, new_url):
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise InstallError(f"source exceeded the {MAX_REDIRECTS}-redirect limit: {redact_url(request.full_url)}")
        return super().redirect_request(request, fp, code, message, headers, new_url)


@dataclass
class DownloadBudget:
    total_bytes: int = 0
    responses: int = 0

    def reserve_response(self) -> None:
        if self.responses >= MAX_CRAWL_PAGES + MAX_FILES:
            raise InstallError("remote source exceeded its request limit")
        self.responses += 1

    def remaining_bytes(self) -> int:
        return MAX_TOTAL_BYTES - self.total_bytes

    def record(self, amount: int) -> None:
        self.total_bytes += amount
        if self.total_bytes > MAX_TOTAL_BYTES:
            raise InstallError(f"remote source exceeded its {MAX_TOTAL_BYTES}-byte budget")


class HttpClient:
    def __init__(self) -> None:
        self.github_token = os.environ.get("GITHUB_TOKEN")
        self.insecure_warnings: set[str] = set()
        self.budget: Optional[DownloadBudget] = None

    def start_source(self) -> None:
        self.budget = DownloadBudget()

    def get(self, url: str, *, accept: Optional[str] = None) -> tuple[bytes, str, str]:
        safe_url = redact_url(url)
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise InstallError(f"invalid HTTP(S) source: {safe_url}") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise InstallError(f"invalid HTTP(S) source: {safe_url}")
        warning_key = parsed.netloc.lower()
        if parsed.scheme == "http" and warning_key not in self.insecure_warnings:
            print(f"warning: downloading prompt content over insecure HTTP: {redact_url(url).split('/', 3)[2]}", file=sys.stderr)
            self.insecure_warnings.add(warning_key)
        if self.budget:
            self.budget.reserve_response()

        headers = {"User-Agent": "opencode-dev-kit-installer/1"}
        if accept:
            headers["Accept"] = accept
        if parsed.netloc.lower() in {"api.github.com", "raw.githubusercontent.com"} and self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
            if parsed.netloc.lower() == "api.github.com":
                headers["X-GitHub-Api-Version"] = "2022-11-28"
        try:
            request = Request(url, headers=headers)
        except ValueError as exc:
            raise InstallError(f"invalid HTTP(S) source: {safe_url}") from exc
        handler = RedirectLimitHandler()
        opener = build_opener(handler)
        try:
            with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                final_url = response.geturl()
                final_parts = urlsplit(final_url)
                if final_parts.scheme not in {"http", "https"}:
                    raise InstallError(f"source redirected to an unsupported URL: {redact_url(final_url)}")
                content_length = response.headers.get("Content-Length")
                try:
                    declared_length = int(content_length) if content_length else None
                except ValueError as exc:
                    raise InstallError(f"source returned an invalid Content-Length: {safe_url}") from exc
                response_limit = MAX_RESPONSE_BYTES
                if self.budget:
                    response_limit = min(response_limit, self.budget.remaining_bytes())
                if response_limit <= 0:
                    raise InstallError("remote source exceeded its download budget")
                if declared_length is not None and declared_length > response_limit:
                    raise InstallError(f"source is larger than its download budget: {safe_url}")
                data = read_limited(response, response_limit)
                if self.budget:
                    self.budget.record(len(data))
                content_type = response.headers.get_content_type()
                if final_parts.scheme.lower() == "http" and parsed.scheme.lower() == "https":
                    print(
                        f"warning: HTTPS source redirected to insecure HTTP: {redact_url(final_url)}",
                        file=sys.stderr,
                    )
                return data, final_url, content_type
        except HTTPError as exc:
            detail = f"HTTP {exc.code}"
            if exc.code == 401:
                detail += " (authentication is required or invalid)"
            elif exc.code == 403:
                detail += " (access denied or rate limited)"
            elif exc.code == 404:
                detail += " (source was not found or is private)"
            raise InstallError(f"{detail}: {safe_url}") from exc
        except URLError as exc:
            raise InstallError(f"could not download {safe_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise InstallError(f"timed out downloading {safe_url}") from exc


def read_limited(response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise InstallError(f"download exceeded the {limit}-byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Symlink bundled and external OpenCode agents, skills, and plugins into .opencode/."
    )
    parser.add_argument(
        "--target",
        type=Path,
        help="project directory, or an explicit .opencode directory; defaults to the current project",
    )
    parser.add_argument(
        "--config",
        nargs="?",
        const="config.json",
        metavar="PATH",
        help="merge sources from PATH, or ./config.json when PATH is omitted",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="select an agent-models profile from the explicit config file",
    )
    parser.add_argument(
        "--skill",
        action="append",
        default=[],
        metavar="SOURCE",
        help="add one skill source; repeatable and auto-detected",
    )
    parser.add_argument(
        "--skills",
        nargs="?",
        const=BUNDLED_SENTINEL,
        action="append",
        default=[],
        metavar="SOURCE",
        help="add a skill collection, or select bundled skills when no source is given",
    )
    parser.add_argument(
        "--agent",
        action="append",
        default=[],
        metavar="SOURCE",
        help="add one agent source; repeatable and auto-detected",
    )
    parser.add_argument(
        "--agents",
        nargs="?",
        const=BUNDLED_SENTINEL,
        action="append",
        default=[],
        metavar="SOURCE",
        help="add an agent collection, or select bundled agents when no source is given",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        metavar="SOURCE",
        help="add one JavaScript plugin source; repeatable and auto-detected",
    )
    parser.add_argument(
        "--plugins",
        nargs="?",
        const=BUNDLED_SENTINEL,
        action="append",
        default=[],
        metavar="SOURCE",
        help="add a JavaScript plugin collection, or select bundled plugins when no source is given",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and validate sources without changing the target",
    )
    return parser.parse_args(argv)


def resolve_target(raw_target: Optional[Path]) -> Path:
    if raw_target is None:
        return Path.cwd().resolve() / ".opencode"
    target = raw_target.expanduser()
    if target.name == ".opencode":
        return target.absolute()
    return target.resolve() / ".opencode"


def is_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def resolve_local_path(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise InstallError(f"source does not exist: {path}") from exc


def strip_jsonc_comments(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            output.append(" ")
            index += 2
            closed = False
            while index + 1 < len(text):
                if text[index : index + 2] == "*/":
                    index += 2
                    closed = True
                    break
                if text[index] in "\r\n":
                    output.append(text[index])
                index += 1
            if not closed:
                raise InstallError("unterminated JSONC block comment")
            continue
        output.append(char)
        index += 1
    return "".join(output)


def remove_jsonc_trailing_commas(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def parse_json_document(text: str, source: Path) -> object:
    def reject_nonstandard_number(value: str) -> None:
        raise ValueError(f"non-standard JSON number: {value}")

    try:
        return json.loads(
            remove_jsonc_trailing_commas(strip_jsonc_comments(text)),
            parse_constant=reject_nonstandard_number,
        )
    except json.JSONDecodeError as exc:
        raise InstallError(f"invalid JSON/JSONC in {source}: line {exc.lineno}, column {exc.colno}") from exc
    except ValueError as exc:
        raise InstallError(f"invalid JSON/JSONC in {source}: {exc}") from exc


def validate_provider_overlay(provider_id: str, provider: dict[str, object]) -> None:
    allowed_provider_fields = {"api", "name", "env", "id", "npm", "whitelist", "blacklist", "options", "models"}
    unknown_provider_fields = set(provider) - allowed_provider_fields
    if unknown_provider_fields:
        raise InstallError(
            f"provider {provider_id!r} contains unsupported fields: "
            + ", ".join(sorted(unknown_provider_fields))
        )
    for field in ("api", "name", "id", "npm"):
        if field in provider and not isinstance(provider[field], str):
            raise InstallError(f"provider {provider_id!r}.{field} must be a string")
    for field in ("env", "whitelist", "blacklist"):
        if field in provider and (
            not isinstance(provider[field], list)
            or not all(isinstance(item, str) for item in provider[field])
        ):
            raise InstallError(f"provider {provider_id!r}.{field} must be an array of strings")
    if "options" in provider:
        options = provider["options"]
        if not isinstance(options, dict):
            raise InstallError(f"provider {provider_id!r}.options must be an object")
        for field in ("apiKey", "baseURL", "enterpriseUrl"):
            if field in options and not isinstance(options[field], str):
                raise InstallError(f"provider {provider_id!r}.options.{field} must be a string")
        if "setCacheKey" in options and not isinstance(options["setCacheKey"], bool):
            raise InstallError(f"provider {provider_id!r}.options.setCacheKey must be boolean")
        for field in ("timeout", "headerTimeout", "chunkTimeout"):
            if field in options and options[field] is not False and (
                not isinstance(options[field], int) or isinstance(options[field], bool) or options[field] <= 0
            ):
                raise InstallError(f"provider {provider_id!r}.options.{field} must be a positive integer or false")

    models = provider.get("models")
    if models is None:
        return
    if not isinstance(models, dict):
        raise InstallError(f"provider {provider_id!r}.models must be an object")
    allowed_model_fields = {
        "id", "name", "family", "release_date", "attachment", "reasoning", "temperature", "tool_call",
        "interleaved", "cost", "limit", "modalities", "experimental", "status", "provider", "options",
        "headers", "variants",
    }
    for model_id, model in models.items():
        if not isinstance(model_id, str) or not model_id.strip():
            raise InstallError(f"provider {provider_id!r} has an invalid model ID")
        if not isinstance(model, dict):
            raise InstallError(f"provider {provider_id!r}.models[{model_id!r}] must be an object")
        unknown_model_fields = set(model) - allowed_model_fields
        if unknown_model_fields:
            raise InstallError(
                f"provider {provider_id!r}.models[{model_id!r}] contains unsupported fields: "
                + ", ".join(sorted(unknown_model_fields))
            )
        for field in ("id", "name", "family", "release_date"):
            if field in model and not isinstance(model[field], str):
                raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].{field} must be a string")
        for field in ("attachment", "reasoning", "temperature", "tool_call", "experimental"):
            if field in model and not isinstance(model[field], bool):
                raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].{field} must be boolean")
        if "status" in model and (
            not isinstance(model["status"], str)
            or model["status"] not in {"alpha", "beta", "deprecated", "active"}
        ):
            raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].status is invalid")
        if "interleaved" in model:
            interleaved = model["interleaved"]
            if isinstance(interleaved, dict):
                if set(interleaved) != {"field"} or not isinstance(interleaved["field"], str):
                    raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].interleaved is invalid")
            elif not isinstance(interleaved, (bool, str)):
                raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].interleaved is invalid")
        if "provider" in model:
            nested_provider = model["provider"]
            if not isinstance(nested_provider, dict) or set(nested_provider) - {"npm", "api"}:
                raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].provider is invalid")
            if any(not isinstance(value, str) for value in nested_provider.values()):
                raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].provider values must be strings")
        if "headers" in model:
            headers = model["headers"]
            if not isinstance(headers, dict) or any(
                not isinstance(key, str) or not isinstance(value, str) for key, value in headers.items()
            ):
                raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].headers must be a string map")
        for field in ("options", "variants"):
            if field in model and (
                not isinstance(model[field], dict)
                or (field == "variants" and not all(isinstance(value, dict) for value in model[field].values()))
            ):
                raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].{field} is invalid")
        if "variants" in model:
            for variant_name, variant in model["variants"].items():
                if "disabled" in variant and not isinstance(variant["disabled"], bool):
                    raise InstallError(
                        f"provider {provider_id!r}.models[{model_id!r}].variants[{variant_name!r}].disabled must be boolean"
                    )
        if "modalities" in model:
            modalities = model["modalities"]
            allowed_modalities = {"text", "audio", "image", "video", "pdf"}
            if not isinstance(modalities, dict) or not set(modalities).issubset({"input", "output"}):
                raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].modalities must be an object")
            for direction in ("input", "output"):
                if direction in modalities and (
                    not isinstance(modalities[direction], list)
                    or not all(isinstance(item, str) for item in modalities[direction])
                    or not set(modalities[direction]).issubset(allowed_modalities)
                ):
                    raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].modalities.{direction} is invalid")
        if "limit" in model:
            limit = model["limit"]
            if not isinstance(limit, dict) or set(limit) - {"context", "input", "output"} or not {"context", "output"}.issubset(limit):
                raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].limit is invalid")
            if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in limit.values()):
                raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].limit values must be numeric")
        if "cost" in model:
            cost = model["cost"]
            allowed_cost = {"input", "output", "cache_read", "cache_write", "context_over_200k"}
            if not isinstance(cost, dict) or set(cost) - allowed_cost or not {"input", "output"}.issubset(cost):
                raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].cost is invalid")
            for key, value in cost.items():
                if key != "context_over_200k" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                    raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].cost values must be numeric")
                if key == "context_over_200k":
                    if not isinstance(value, dict) or set(value) - {"input", "output", "cache_read", "cache_write"} or not {"input", "output"}.issubset(value):
                        raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].cost.context_over_200k is invalid")
                    if any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in value.values()):
                        raise InstallError(f"provider {provider_id!r}.models[{model_id!r}].cost values must be numeric")


def normalize_alias(alias: str, context: str) -> str:
    if not isinstance(alias, str) or not alias or alias != alias.strip() or "/" in alias or "\\" in alias:
        raise InstallError(f"{context} must be a non-empty alias without path separators")
    if len(alias) > 64:
        raise InstallError(f"{context} must be 64 characters or fewer")
    return alias.casefold()


def validate_model_reference(value: str, context: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value.strip():
        raise InstallError(f"{context} must be a non-empty provider/model ID")
    provider_id, separator, model_id = value.partition("/")
    if not separator or not provider_id or not model_id or any(char.isspace() for char in value):
        raise InstallError(f"{context} must use provider/model-id format: {value!r}")
    return value


def validate_model_aliases(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InstallError("config field 'model-aliases' must be an object")
    aliases: dict[str, object] = {}
    seen: set[str] = set()
    for alias, definition in value.items():
        normalized = normalize_alias(alias, "model alias")
        if normalized in seen:
            raise InstallError(f"duplicate model alias: {alias!r}")
        seen.add(normalized)
        if isinstance(definition, str):
            validate_model_reference(definition, f"model-aliases[{alias!r}]")
        elif isinstance(definition, dict):
            if set(definition) - {"model", "variant"}:
                raise InstallError(f"model-aliases[{alias!r}] contains unknown keys")
            validate_model_reference(definition.get("model"), f"model-aliases[{alias!r}].model")
            if "variant" in definition and (
                not isinstance(definition["variant"], str) or not definition["variant"].strip()
            ):
                raise InstallError(f"model-aliases[{alias!r}].variant must be a non-empty string")
        else:
            raise InstallError(f"model-aliases[{alias!r}] must be a model ID or object")
        aliases[normalized] = definition
    return aliases


def validate_agent_model_assignments(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InstallError("config field 'agent-models' must be an object")
    assignments: dict[str, object] = {}
    for agent_name, definition in value.items():
        if not isinstance(agent_name, str) or not agent_name.strip() or "/" in agent_name:
            raise InstallError(f"agent-models key {agent_name!r} is invalid")
        if isinstance(definition, str):
            if not definition.strip():
                raise InstallError(f"agent-models[{agent_name!r}] must not be empty")
        elif isinstance(definition, dict):
            if set(definition) - {"alias", "model", "variant"} or ("alias" in definition and "model" in definition):
                raise InstallError(f"agent-models[{agent_name!r}] must contain alias or model, not both")
            if "alias" not in definition and "model" not in definition:
                raise InstallError(f"agent-models[{agent_name!r}] must contain alias or model")
            if "alias" in definition:
                normalize_alias(definition["alias"], f"agent-models[{agent_name!r}].alias")
            if "model" in definition:
                validate_model_reference(definition["model"], f"agent-models[{agent_name!r}].model")
            if "variant" in definition and (
                not isinstance(definition["variant"], str) or not definition["variant"].strip()
            ):
                raise InstallError(f"agent-models[{agent_name!r}].variant must be a non-empty string")
        else:
            raise InstallError(f"agent-models[{agent_name!r}] must be a string or object")
        assignments[agent_name] = definition
    return assignments


def validate_agent_model_profiles(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict):
        raise InstallError("config field 'agent-models' must be an object of profiles")
    profiles: dict[str, dict[str, object]] = {}
    seen: set[str] = set()
    for profile_name, assignments in value.items():
        normalized = normalize_alias(profile_name, "agent-models profile")
        if normalized in seen:
            raise InstallError(f"duplicate agent-models profile: {profile_name!r}")
        seen.add(normalized)
        if isinstance(assignments, dict) and set(assignments) & {"alias", "model", "variant"}:
            raise InstallError(
                "agent-models must be a profile map; the old flat agent-to-model shape is not supported"
            )
        profiles[profile_name] = validate_agent_model_assignments(assignments)
    return profiles


def load_config(path: Path) -> InstallerConfig:
    try:
        data = parse_json_document(path.read_text(encoding="utf-8"), path)
    except FileNotFoundError as exc:
        raise InstallError(f"config file does not exist: {path}") from exc
    if not isinstance(data, dict):
        raise InstallError("config root must be a JSON object")
    unknown = set(data) - {"skills", "agents", "plugins", "model-aliases", "agent-models", "opencode_json"}
    if unknown:
        raise InstallError(f"config contains unknown keys: {', '.join(sorted(unknown))}")

    def read_sources(key: str) -> list[str]:
        values = data.get(key, [])
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise InstallError(f"config field {key!r} must be an array of strings")
        return values

    opencode_json = data.get("opencode_json", {})
    if not isinstance(opencode_json, dict):
        raise InstallError("config field 'opencode_json' must be an object")
    unknown_opencode = set(opencode_json) - {"providers"}
    if unknown_opencode:
        raise InstallError(
            "config field 'opencode_json' contains unknown keys: "
            + ", ".join(sorted(unknown_opencode))
        )
    providers = opencode_json.get("providers", {})
    if not isinstance(providers, dict):
        raise InstallError("config field 'opencode_json.providers' must be an object")
    for provider_id, provider in providers.items():
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise InstallError("provider IDs in opencode_json.providers must be non-empty strings")
        if not isinstance(provider, dict):
            raise InstallError(f"provider {provider_id!r} must be an object")
        validate_provider_overlay(provider_id, provider)

    return InstallerConfig(
        read_sources("skills"),
        read_sources("agents"),
        read_sources("plugins"),
        providers,
        validate_model_aliases(data.get("model-aliases", {})),
        validate_agent_model_profiles(data.get("agent-models", {})),
    )


def build_sources(
    args: argparse.Namespace,
) -> tuple[list[SourceSpec], dict[str, dict[str, object]], dict[str, object], dict[str, dict[str, object]]]:
    sources = [
        SourceSpec("skill", str(SCRIPT_DIR / "skills"), "bundled", bundled=True),
        SourceSpec("agent", str(SCRIPT_DIR / "agents"), "bundled", bundled=True),
    ]
    bundled_plugins = SCRIPT_DIR / "plugins"
    if bundled_plugins.is_dir():
        sources.append(SourceSpec("plugin", str(bundled_plugins), "bundled", bundled=True))
    if args.profile and args.config is None:
        raise InstallError("--profile requires an explicit --config PATH")
    provider_overlays: dict[str, dict[str, object]] = {}
    model_aliases: dict[str, object] = {}
    agent_profiles: dict[str, dict[str, object]] = {}
    config_base = Path.cwd()
    if args.config is not None:
        config_path = Path(args.config).expanduser()
        if not config_path.is_absolute():
            config_path = Path.cwd() / config_path
        config_path = config_path.resolve()
        config = load_config(config_path)
        config_base = config_path.parent
        provider_overlays = config.providers
        model_aliases = config.model_aliases
        agent_profiles = config.agent_models
        sources.extend(
            SourceSpec("skill", resolve_source_value(value, config_base), f"config:{config_path}")
            for value in config.skills
        )
        sources.extend(
            SourceSpec("agent", resolve_source_value(value, config_base), f"config:{config_path}")
            for value in config.agents
        )
        sources.extend(
            SourceSpec("plugin", resolve_source_value(value, config_base), f"config:{config_path}")
            for value in config.plugins
        )

    for value in args.skills:
        if value != BUNDLED_SENTINEL:
            sources.append(SourceSpec("skill", resolve_source_value(value, Path.cwd()), "command line"))
    for value in args.skill:
        sources.append(SourceSpec("skill", resolve_source_value(value, Path.cwd()), "command line"))
    for value in args.agents:
        if value != BUNDLED_SENTINEL:
            sources.append(SourceSpec("agent", resolve_source_value(value, Path.cwd()), "command line"))
    for value in args.agent:
        sources.append(SourceSpec("agent", resolve_source_value(value, Path.cwd()), "command line"))
    for value in args.plugins:
        if value != BUNDLED_SENTINEL:
            sources.append(SourceSpec("plugin", resolve_source_value(value, Path.cwd()), "command line"))
    for value in args.plugin:
        sources.append(SourceSpec("plugin", resolve_source_value(value, Path.cwd()), "command line"))
    return deduplicate_sources(sources), provider_overlays, model_aliases, agent_profiles


def resolve_source_value(value: str, base_dir: Path) -> str:
    if not value.strip():
        raise InstallError("source must not be empty")
    return value if is_url(value) else str(resolve_local_path(value, base_dir))


def deep_merge(existing: object, incoming: object) -> object:
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = dict(existing)
        for key, value in incoming.items():
            merged[key] = deep_merge(merged[key], value) if key in merged else value
        return merged
    return incoming


def resolve_agent_routing(
    artifacts: list[Artifact],
    model_aliases: dict[str, object],
    assignments: dict[str, object],
) -> dict[str, dict[str, str]]:
    if not assignments:
        return {}
    unsupported_agents = set(assignments) - PROFILE_MANAGED_AGENTS
    if unsupported_agents:
        primary_agents = unsupported_agents & {"plan", "build"}
        if primary_agents:
            raise InstallError(
                "agent-models cannot override manually managed primary agents: "
                + ", ".join(sorted(primary_agents))
            )
        raise InstallError(
            "agent-models supports only bundled delegated agents: "
            + ", ".join(sorted(PROFILE_MANAGED_AGENTS))
        )
    installed_agents = {artifact.name for artifact in artifacts if artifact.kind == "agent"}
    agent_modes = {
        artifact.name: artifact.agent_mode or "all"
        for artifact in artifacts
        if artifact.kind == "agent"
    }
    unknown_agents = set(assignments) - installed_agents
    if unknown_agents:
        raise InstallError("agent-models references unknown agents: " + ", ".join(sorted(unknown_agents)))

    def resolve_alias(alias: str, context: str) -> tuple[str, Optional[str]]:
        normalized = normalize_alias(alias, context)
        if normalized not in model_aliases:
            raise InstallError(f"{context} references unknown model alias: {alias!r}")
        definition = model_aliases[normalized]
        if isinstance(definition, str):
            return definition, None
        return definition["model"], definition.get("variant")

    resolved: dict[str, dict[str, str]] = {}
    for agent_name, definition in assignments.items():
        alias_variant: Optional[str] = None
        if isinstance(definition, str):
            normalized = definition.casefold()
            if normalized in model_aliases:
                model, alias_variant = resolve_alias(definition, f"agent-models[{agent_name!r}]")
            else:
                model = validate_model_reference(definition, f"agent-models[{agent_name!r}]")
        else:
            if "alias" in definition:
                model, alias_variant = resolve_alias(definition["alias"], f"agent-models[{agent_name!r}].alias")
            else:
                model = validate_model_reference(definition["model"], f"agent-models[{agent_name!r}].model")
        variant = definition.get("variant") if isinstance(definition, dict) else alias_variant
        if variant is None and alias_variant is not None:
            variant = alias_variant
        resolved[agent_name] = {
            "mode": agent_modes[agent_name],
            "model": model,
            **({"variant": variant} if variant else {}),
        }
    return resolved


def merge_provider_config(
    target: Path,
    provider_overlays: dict[str, dict[str, object]],
    agent_routing: dict[str, dict[str, str]],
) -> Optional[dict[str, object]]:
    if not provider_overlays and not agent_routing:
        return None
    if target.is_symlink():
        raise InstallError(f"installation target must not be a symlink: {target}")
    config_path = target / "opencode.json"
    if config_path.is_symlink():
        raise InstallError(f"runtime OpenCode config must not be a symlink: {config_path}")
    if config_path.exists():
        if not config_path.is_file():
            raise InstallError(f"runtime OpenCode config is not a file: {config_path}")
        current = parse_json_document(config_path.read_text(encoding="utf-8"), config_path)
    else:
        current = {"$schema": "https://opencode.ai/config.json"}
    if not isinstance(current, dict):
        raise InstallError(f"runtime OpenCode config must be a JSON object: {config_path}")

    merged = dict(current)
    if provider_overlays:
        existing_providers = current.get("provider", {})
        if not isinstance(existing_providers, dict):
            raise InstallError(f"runtime OpenCode config field 'provider' must be an object: {config_path}")
        disabled = current.get("disabled_providers", [])
        if not isinstance(disabled, list) or not all(isinstance(item, str) for item in disabled):
            raise InstallError("runtime OpenCode config field 'disabled_providers' must be an array of strings")
        for provider_id in provider_overlays:
            if provider_id in disabled:
                raise InstallError(
                    f"provider {provider_id!r} is disabled in {config_path}; remove it from disabled_providers first"
                )
        merged["provider"] = deep_merge(existing_providers, provider_overlays)
        enabled = current.get("enabled_providers")
        if enabled is not None:
            if not isinstance(enabled, list) or not all(isinstance(item, str) for item in enabled):
                raise InstallError("runtime OpenCode config field 'enabled_providers' must be an array of strings")
            merged["enabled_providers"] = list(enabled)
            for provider_id in provider_overlays:
                if provider_id not in merged["enabled_providers"]:
                    merged["enabled_providers"].append(provider_id)
    if agent_routing:
        existing_agents = current.get("agent", {})
        if not isinstance(existing_agents, dict):
            raise InstallError(f"runtime OpenCode config field 'agent' must be an object: {config_path}")
        merged_agents = dict(existing_agents)
        for agent_name, routing in agent_routing.items():
            existing_agent = merged_agents.get(agent_name, {})
            if not isinstance(existing_agent, dict):
                raise InstallError(f"runtime OpenCode config agent {agent_name!r} must be an object")
            updated_agent = dict(existing_agent)
            updated_agent["mode"] = routing["mode"]
            updated_agent["model"] = routing["model"]
            if "variant" in routing:
                updated_agent["variant"] = routing["variant"]
            else:
                updated_agent.pop("variant", None)
            merged_agents[agent_name] = updated_agent
        merged["agent"] = merged_agents
    return merged


def deduplicate_sources(sources: Iterable[SourceSpec]) -> list[SourceSpec]:
    result: list[SourceSpec] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        key = (source.kind, source.value)
        if key in seen:
            continue
        seen.add(key)
        result.append(source)
    return result


def parse_frontmatter(data: bytes, source: str) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError(f"{source} is not valid UTF-8") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise InstallError(f"{source} must start with YAML frontmatter")
    try:
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise InstallError(f"{source} has unterminated YAML frontmatter") from exc
    if not any(line.strip() for line in lines[closing + 1 :]):
        raise InstallError(f"{source} has an empty Markdown body")

    fields: dict[str, str] = {}
    frontmatter_lines = lines[1:closing]
    index = 0
    while index < len(frontmatter_lines):
        line = frontmatter_lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#") or line.startswith((" ", "\t")):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:\s*(.*))?$", line)
        if not match:
            continue
        key, value = match.group(1), (match.group(2) or "").strip()
        if value in {"|", ">", "|-", ">-", "|+", ">+"}:
            block: list[str] = []
            while index < len(frontmatter_lines) and (
                frontmatter_lines[index].startswith((" ", "\t")) or not frontmatter_lines[index].strip()
            ):
                block.append(frontmatter_lines[index].strip())
                index += 1
            fields[key] = ("\n" if value.startswith("|") else " ").join(block).strip()
        else:
            fields[key] = unquote_scalar(value)
    return fields


def unquote_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def validate_name(name: str, context: str) -> None:
    if len(name) > 64 or not SAFE_NAME.fullmatch(name):
        raise InstallError(f"{context} has invalid name {name!r}; use lowercase hyphen-separated names")


def validate_agent(data: bytes, filename: str) -> str:
    fields = parse_frontmatter(data, filename)
    description = fields.get("description", "").strip()
    if not description:
        raise InstallError(f"{filename} must have a non-empty description")
    mode = fields.get("mode")
    if mode and mode not in VALID_AGENT_MODES:
        raise InstallError(f"{filename} has invalid mode {mode!r}")
    name = Path(filename).stem
    validate_name(name, filename)
    return name


def agent_mode(data: bytes, filename: str) -> str:
    fields = parse_frontmatter(data, filename)
    mode = fields.get("mode", "all").strip() or "all"
    if mode not in VALID_AGENT_MODES:
        raise InstallError(f"{filename} has invalid mode {mode!r}")
    return mode


def validate_skill(data: bytes, root_name: str, source: str) -> str:
    fields = parse_frontmatter(data, source)
    name = fields.get("name", "").strip()
    description = fields.get("description", "").strip()
    if not name or not description:
        raise InstallError(f"{source} must have non-empty name and description")
    validate_name(name, source)
    if root_name and name != root_name:
        raise InstallError(f"{source} declares name {name!r}, but its directory is {root_name!r}")
    return name


def validate_relative_path(value: str, context: str) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise InstallError(f"{context} contains an unsafe path: {value!r}")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value) or value.startswith("//"):
        raise InstallError(f"{context} contains an absolute path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InstallError(f"{context} contains an unsafe path: {value!r}")
    for part in parts:
        if part.endswith((".", " ")) or ":" in part:
            raise InstallError(f"{context} contains a platform-ambiguous path: {value!r}")
        reserved = part.split(".", 1)[0].upper()
        if reserved in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", reserved):
            raise InstallError(f"{context} contains a reserved path component: {value!r}")
    if value == ".install-manifest.json":
        raise InstallError(f"{context} uses a reserved installer path: {value!r}")
    return PurePosixPath(*parts).as_posix()


def safe_relative_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise InstallError(f"source path escapes its root: {path}") from exc
    return validate_relative_path(PurePosixPath(*relative.parts).as_posix(), str(path))


def collect_local_files(root: Path) -> dict[str, bytes]:
    if root.is_symlink():
        raise InstallError(f"skill root must not be a symlink: {root}")
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise InstallError(f"skill contains an unsupported symlink: {path}")
        if not path.is_file():
            continue
        relative = safe_relative_path(path, root)
        if len(files) >= MAX_FILES:
            raise InstallError(f"skill contains more than {MAX_FILES} files: {root}")
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise InstallError(f"file exceeds {MAX_FILE_BYTES} bytes: {path}")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise InstallError(f"skill exceeds {MAX_TOTAL_BYTES} total bytes: {root}")
        files[relative] = data
    return files


def make_skill_artifact(
    files: dict[str, bytes], root_name: str, origin: str, bundled: bool, link_source: Optional[Path]
) -> Artifact:
    skill_data = files.get("SKILL.md")
    if skill_data is None:
        raise InstallError(f"skill source has no direct SKILL.md: {origin}")
    name = validate_skill(skill_data, root_name, f"{origin}/SKILL.md")
    artifact = Artifact("skill", name, files, origin, bundled, link_source)
    artifact.content_hash = artifact.calculate_hash()
    return artifact


def validate_plugin_filename(filename: str, context: str) -> str:
    if Path(filename).name != filename or not filename.endswith(".js"):
        raise InstallError(f"{context} must be a JavaScript file: {filename}")
    return validate_relative_path(filename, context)


def make_plugin_artifact(
    data: bytes,
    filename: str,
    origin: str,
    bundled: bool,
    link_source: Optional[Path],
) -> Artifact:
    safe_filename = validate_plugin_filename(filename, origin)
    if len(data) > MAX_FILE_BYTES:
        raise InstallError(f"plugin exceeds {MAX_FILE_BYTES} bytes: {origin}")
    artifact = Artifact("plugin", safe_filename, {safe_filename: data}, origin, bundled, link_source)
    artifact.content_hash = artifact.calculate_hash()
    return artifact


def make_agent_artifact(
    data: bytes,
    filename: str,
    origin: str,
    bundled: bool,
    link_source: Optional[Path],
) -> Artifact:
    name = validate_agent(data, filename)
    artifact = Artifact("agent", name, {Path(filename).name: data}, origin, bundled, link_source)
    artifact.agent_mode = agent_mode(data, filename)
    artifact.content_hash = artifact.calculate_hash()
    return artifact


def resolve_local_plugin_source(source: SourceSpec) -> list[Artifact]:
    path = Path(source.value)
    if path.is_file():
        return [make_plugin_artifact(path.read_bytes(), path.name, str(path), source.bundled, path)]
    if not path.is_dir():
        raise InstallError(f"plugin source is not a file or directory: {path}")
    files: list[Path] = []
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise InstallError(f"plugin source contains an unsupported symlink: {child}")
        if child.is_file() and child.suffix.lower() == ".js":
            files.append(child)
    if not files:
        raise InstallError(f"plugin source contains no JavaScript files: {path}")
    if len(files) > MAX_COLLECTION_ITEMS:
        raise InstallError(f"plugin collection exceeds {MAX_COLLECTION_ITEMS} items: {path}")
    return [
        make_plugin_artifact(file_path.read_bytes(), file_path.name, str(file_path), source.bundled, file_path)
        for file_path in files
    ]


def resolve_local_source(source: SourceSpec) -> list[Artifact]:
    path = Path(source.value)
    if source.kind == "plugin":
        return resolve_local_plugin_source(source)
    if source.kind == "skill":
        if path.is_file():
            if path.name != "SKILL.md":
                raise InstallError(f"single skill files must be named SKILL.md: {path}")
            files = {"SKILL.md": path.read_bytes()}
            return [make_skill_artifact(files, path.parent.name, str(path.parent), source.bundled, path.parent)]
        if (path / "SKILL.md").is_file():
            return [make_skill_artifact(collect_local_files(path), path.name, str(path), source.bundled, path)]
        children = [child for child in sorted(path.iterdir()) if child.is_dir() and (child / "SKILL.md").is_file()]
        if not children:
            raise InstallError(f"skill source is neither one skill nor a collection: {path}")
        if len(children) > MAX_COLLECTION_ITEMS:
            raise InstallError(f"skill collection exceeds {MAX_COLLECTION_ITEMS} items: {path}")
        return [
            make_skill_artifact(collect_local_files(child), child.name, str(child), source.bundled, child)
            for child in children
        ]

    if path.is_file():
        if path.suffix.lower() != ".md":
            raise InstallError(f"agent source must be a Markdown file: {path}")
        data = path.read_bytes()
        return [make_agent_artifact(data, path.name, str(path), source.bundled, path)]
    files = [child for child in sorted(path.iterdir()) if child.is_file() and child.suffix.lower() == ".md"]
    if not files:
        raise InstallError(f"agent source is not a collection of Markdown files: {path}")
    if len(files) > MAX_COLLECTION_ITEMS:
        raise InstallError(f"agent collection exceeds {MAX_COLLECTION_ITEMS} items: {path}")
    return [
        make_agent_artifact(file_path.read_bytes(), file_path.name, str(file_path), source.bundled, file_path)
        for file_path in files
    ]


def parse_github_url(url: str) -> Optional[tuple[str, str, str, str, str]]:
    parsed = urlsplit(url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 4 or parts[2] not in {"tree", "blob"}:
        return None
    owner, repo, view = parts[0], parts[1], parts[2]
    query_ref = parse_qs(parsed.query).get("ref", [None])[0]
    ref = unquote(query_ref) if query_ref else parts[3]
    path_start = 4
    path = "/".join(parts[path_start:])
    return owner, repo, view, ref, path


def github_api_url(owner: str, repo: str, path: str, ref: str) -> str:
    encoded_path = quote(path, safe="/")
    return f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/contents/{encoded_path}?ref={quote(ref, safe='')}"


def github_json(client: HttpClient, url: str) -> object:
    data, _, content_type = client.get(url, accept="application/vnd.github+json")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"GitHub returned invalid JSON for {redact_url(url)} ({content_type})") from exc


def github_contents(client: HttpClient, owner: str, repo: str, path: str, ref: str) -> object:
    return github_json(client, github_api_url(owner, repo, path, ref))


def github_file(client: HttpClient, owner: str, repo: str, path: str, ref: str) -> bytes:
    url = github_api_url(owner, repo, path, ref)
    data, _, _ = client.get(url, accept="application/vnd.github.raw+json")
    if len(data) > MAX_FILE_BYTES:
        raise InstallError(f"GitHub file exceeds {MAX_FILE_BYTES} bytes: {path}")
    return data


def github_recursive_tree(client: HttpClient, owner: str, repo: str, ref: str) -> tuple[str, list[dict[str, str]]]:
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
        commit_sha = ref
    else:
        commit_url = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/commits/{quote(ref, safe='')}"
        commit = github_json(client, commit_url)
        commit_sha = (
            commit.get("sha")
            if isinstance(commit, dict)
            else None
        )
        if not isinstance(commit_sha, str):
            raise InstallError(f"GitHub did not return a commit for ref {ref!r}")
    tree_url = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/git/trees/{commit_sha}?recursive=1"
    tree = github_json(client, tree_url)
    if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
        raise InstallError(f"GitHub returned an invalid repository tree for {owner}/{repo}")
    if tree.get("truncated"):
        raise InstallError(f"GitHub repository tree is truncated; narrow the source path: {owner}/{repo}")
    entries: list[dict[str, str]] = []
    for entry in tree["tree"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("type"), str):
            raise InstallError(f"GitHub returned an invalid tree entry for {owner}/{repo}")
        entries.append(entry)
    return commit_sha, entries


def github_raw_file(client: HttpClient, owner: str, repo: str, commit_sha: str, path: str) -> bytes:
    raw_url = f"https://raw.githubusercontent.com/{quote(owner)}/{quote(repo)}/{commit_sha}/{quote(path, safe='/')}"
    data, _, _ = client.get(raw_url)
    if len(data) > MAX_FILE_BYTES:
        raise InstallError(f"GitHub file exceeds {MAX_FILE_BYTES} bytes: {path}")
    return data


def github_tree_files(
    client: HttpClient, owner: str, repo: str, ref: str, root_path: str
) -> dict[str, bytes]:
    commit_sha, entries = github_recursive_tree(client, owner, repo, ref)
    root = PurePosixPath(root_path)
    prefix = root.as_posix().rstrip("/") + "/"
    files: dict[str, bytes] = {}
    total_bytes = 0
    for entry in entries:
        entry_path = PurePosixPath(entry["path"])
        if entry_path.as_posix() != root.as_posix() and not entry_path.as_posix().startswith(prefix):
            continue
        entry_type = entry["type"]
        if entry_type in {"tree"}:
            continue
        if entry_type != "blob" or entry.get("mode") in {"120000", "160000"}:
            raise InstallError(f"GitHub source contains unsupported entry: {entry_path}")
        relative = validate_relative_path(entry_path.relative_to(root).as_posix(), f"GitHub source {root_path}")
        if len(files) >= MAX_FILES:
            raise InstallError(f"GitHub source contains more than {MAX_FILES} files: {root_path}")
        data = github_raw_file(client, owner, repo, commit_sha, entry_path.as_posix())
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            raise InstallError(f"GitHub source exceeds {MAX_TOTAL_BYTES} total bytes: {root_path}")
        files[relative] = data
    if not files:
        raise InstallError(f"GitHub source directory is empty or missing: {root_path}")
    return files


def github_tree_skill_artifacts(
    files: dict[str, bytes], root_path: str, origin: str
) -> list[Artifact]:
    if "SKILL.md" in files:
        return [make_skill_artifact(files, PurePosixPath(root_path).name, origin, False, None)]

    skill_roots = {
        PurePosixPath(relative).parts[0]
        for relative in files
        if len(PurePosixPath(relative).parts) == 2 and PurePosixPath(relative).name == "SKILL.md"
    }
    artifacts: list[Artifact] = []
    for skill_root in sorted(skill_roots):
        prefix = f"{skill_root}/"
        grouped = {
            relative[len(prefix) :]: data
            for relative, data in files.items()
            if relative.startswith(prefix)
        }
        artifacts.append(make_skill_artifact(grouped, skill_root, origin, False, None))
    if not artifacts:
        raise InstallError(f"GitHub tree contains no direct skill directories: {origin}")
    if len(artifacts) > MAX_COLLECTION_ITEMS:
        raise InstallError(f"GitHub skill collection exceeds {MAX_COLLECTION_ITEMS} items: {origin}")
    return artifacts


def github_walk(
    client: HttpClient, owner: str, repo: str, path: str, ref: str, depth: int = 0
) -> dict[str, bytes]:
    if depth > MAX_CRAWL_DEPTH:
        raise InstallError(f"GitHub source exceeded the {MAX_CRAWL_DEPTH}-directory depth limit: {path}")
    listing = github_contents(client, owner, repo, path, ref)
    if isinstance(listing, dict) and listing.get("type") == "file":
        relative = Path(path).name
        return {relative: github_file(client, owner, repo, path, ref)}
    if not isinstance(listing, list):
        raise InstallError(f"GitHub path is not a readable directory: {path}")
    files: dict[str, bytes] = {}
    root = PurePosixPath(path)
    for entry in listing:
        if not isinstance(entry, dict):
            raise InstallError(f"GitHub returned an invalid directory entry under {path}")
        entry_type = entry.get("type")
        entry_path = entry.get("path")
        if not isinstance(entry_path, str) or not entry_path.startswith(str(root)):
            raise InstallError(f"GitHub returned an unsafe path under {path}")
        if entry_type == "file":
            relative = validate_relative_path(
                PurePosixPath(entry_path).relative_to(root).as_posix(), f"GitHub source {path}"
            )
            files[relative] = github_file(client, owner, repo, entry_path, ref)
        elif entry_type == "dir":
            nested = github_walk(client, owner, repo, entry_path, ref, depth + 1)
            prefix = PurePosixPath(entry_path).relative_to(root)
            for nested_path, data in nested.items():
                files[(prefix / nested_path).as_posix()] = data
        elif entry_type in {"symlink", "submodule"}:
            raise InstallError(f"GitHub source contains unsupported {entry_type}: {entry_path}")
        else:
            raise InstallError(f"GitHub returned unsupported entry type {entry_type!r}: {entry_path}")
        if len(files) > MAX_FILES:
            raise InstallError(f"GitHub source contains more than {MAX_FILES} files: {path}")
    total = sum(len(data) for data in files.values())
    if total > MAX_TOTAL_BYTES:
        raise InstallError(f"GitHub source exceeds {MAX_TOTAL_BYTES} total bytes: {path}")
    return files


def resolve_github_source(source: SourceSpec, parsed: tuple[str, str, str, str, str], client: HttpClient) -> list[Artifact]:
    owner, repo, view, ref, path = parsed
    safe_origin = redact_url(source.value)
    if not path:
        raise InstallError(f"GitHub source must identify a file or directory: {safe_origin}")
    if view == "blob":
        if source.kind == "plugin" and PurePosixPath(path).suffix.lower() == ".js":
            data = github_file(client, owner, repo, path, ref)
            return [make_plugin_artifact(data, PurePosixPath(path).name, safe_origin, False, None)]
        if source.kind == "skill" and PurePosixPath(path).name == "SKILL.md":
            root_path = str(PurePosixPath(path).parent)
            files = github_walk(client, owner, repo, root_path, ref)
            return [make_skill_artifact(files, PurePosixPath(root_path).name, safe_origin, False, None)]
        if source.kind != "agent" or PurePosixPath(path).suffix.lower() != ".md":
            raise InstallError(f"GitHub blob does not match the requested {source.kind}: {safe_origin}")
        data = github_file(client, owner, repo, path, ref)
        return [make_agent_artifact(data, PurePosixPath(path).name, safe_origin, False, None)]

    files = github_tree_files(client, owner, repo, ref, path)
    if source.kind == "skill":
        return github_tree_skill_artifacts(files, path, safe_origin)
    if source.kind == "plugin":
        artifacts = [
            make_plugin_artifact(data, Path(relative).name, safe_origin, False, None)
            for relative, data in sorted(files.items())
            if relative.lower().endswith(".js")
        ]
        if not artifacts:
            raise InstallError(f"GitHub tree contains no JavaScript plugins: {safe_origin}")
        if len(artifacts) > MAX_COLLECTION_ITEMS:
            raise InstallError(f"GitHub plugin collection exceeds {MAX_COLLECTION_ITEMS} items: {safe_origin}")
        return artifacts

    artifacts = []
    for relative, data in sorted(files.items()):
        if not relative.lower().endswith(".md"):
            continue
        try:
            artifacts.append(make_agent_artifact(data, Path(relative).name, safe_origin, False, None))
        except InstallError:
            continue
    if not artifacts:
        raise InstallError(f"GitHub tree contains no valid agent Markdown files: {safe_origin}")
    if len(artifacts) > MAX_COLLECTION_ITEMS:
        raise InstallError(f"GitHub agent collection exceeds {MAX_COLLECTION_ITEMS} items: {safe_origin}")
    return artifacts


class LinkParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", "", ""))


def child_url(url: str, base_path: str) -> Optional[str]:
    try:
        candidate = urlsplit(url)
    except ValueError:
        return None
    if candidate.scheme not in {"http", "https"} or not candidate.netloc:
        return None
    decoded_path = unquote(candidate.path)
    if "\\" in decoded_path or "\x00" in decoded_path:
        return None
    normalized = posixpath.normpath(decoded_path)
    if candidate.path.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    prefix = base_path.rstrip("/") + "/"
    if normalized != base_path.rstrip("/") and not normalized.startswith(prefix):
        return None
    encoded_path = quote(normalized, safe="/:@-._~!$&'()*+,;=")
    return urlunsplit((candidate.scheme.lower(), candidate.netloc.lower(), encoded_path, "", ""))


def resolve_generic_http(source: SourceSpec, url: str, client: HttpClient) -> list[Artifact]:
    initial_data, final_url, content_type = client.get(url)
    parsed_final = urlsplit(final_url)
    if parsed_final.path.lower().endswith(".js") and not looks_like_html(initial_data, content_type):
        if source.kind == "plugin":
            return [make_plugin_artifact(initial_data, Path(parsed_final.path).name, redact_url(final_url), False, None)]
        raise InstallError(f"remote source is not an agent or skill: {redact_url(url)}")
    if parsed_final.path.lower().endswith(".md") and not looks_like_html(initial_data, content_type):
        if source.kind == "agent":
            safe_final_url = redact_url(final_url)
            return [make_agent_artifact(initial_data, Path(parsed_final.path).name, safe_final_url, False, None)]
        if Path(parsed_final.path).name == "SKILL.md":
            root_name = PurePosixPath(parsed_final.path).parent.name
            return [make_skill_artifact({"SKILL.md": initial_data}, root_name, redact_url(final_url), False, None)]

    if "json" in content_type or parsed_final.path.lower().endswith("index.json"):
        return resolve_json_collection(source, initial_data, final_url, client)
    if not looks_like_html(initial_data, content_type):
        raise InstallError(f"remote source is not Markdown, JSON, or HTML: {redact_url(url)}")

    base_path = posixpath.normpath(unquote(parsed_final.path)) or "/"
    if not base_path.endswith("/"):
        base_path += "/"
    initial_canonical = canonical_url(final_url)
    queue: list[tuple[str, int, Optional[bytes], str]] = [(initial_canonical, 0, initial_data, content_type)]
    queued = {initial_canonical}
    visited: set[str] = set()
    files: dict[str, bytes] = {}
    total_file_bytes = 0
    pages = 0
    base_origin = (parsed_final.scheme.lower(), parsed_final.netloc.lower())
    while queue:
        current, depth, data, current_type = queue.pop(0)
        queued.discard(current)
        if current in visited:
            continue
        if data is None:
            data, fetched_url, current_type = client.get(current)
            fetched_parts = urlsplit(fetched_url)
            if (fetched_parts.scheme.lower(), fetched_parts.netloc.lower()) != base_origin:
                raise InstallError(f"HTML source redirected outside its origin: {redact_url(current)}")
            if child_url(fetched_url, base_path) is None:
                raise InstallError(f"HTML source redirected outside its supplied path: {redact_url(current)}")
            current = canonical_url(fetched_url)
        visited.add(current)
        pages += 1
        if pages > MAX_CRAWL_PAGES:
            raise InstallError(f"HTML source exceeded the {MAX_CRAWL_PAGES}-page limit: {redact_url(url)}")
        if not looks_like_html(data, current_type):
            if len(files) >= MAX_FILES:
                raise InstallError(f"HTML source contains more than {MAX_FILES} files: {redact_url(url)}")
            if len(data) > MAX_FILE_BYTES or total_file_bytes + len(data) > MAX_TOTAL_BYTES:
                raise InstallError(f"HTML source exceeds its file-size limit: {redact_url(url)}")
            files[current] = data
            total_file_bytes += len(data)
            continue
        parser = LinkParser()
        try:
            parser.feed(data.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise InstallError(f"HTML source is not UTF-8: {redact_url(current)}") from exc
        candidates: list[str] = []
        scheduled: set[str] = set()
        for link in parser.links:
            candidate = urljoin(current, link)
            normalized = child_url(candidate, base_path)
            if not normalized:
                continue
            candidate_parts = urlsplit(normalized)
            if (candidate_parts.scheme, candidate_parts.netloc) != base_origin:
                continue
            canonical = canonical_url(normalized)
            if canonical in visited or canonical in queued or canonical in scheduled:
                continue
            scheduled.add(canonical)
            candidates.append(normalized)
        if depth >= MAX_CRAWL_DEPTH and candidates:
            raise InstallError(f"HTML source exceeded the {MAX_CRAWL_DEPTH}-directory depth limit: {redact_url(url)}")
        if pages + len(queue) + len(candidates) > MAX_CRAWL_PAGES:
            raise InstallError(f"HTML source exceeded the {MAX_CRAWL_PAGES}-page limit: {redact_url(url)}")
        for normalized in candidates:
            canonical_next = canonical_url(normalized)
            if canonical_next in visited or canonical_next in queued:
                continue
            queue.append((canonical_next, depth + 1, None, ""))
            queued.add(canonical_next)

    if source.kind == "skill":
        return group_crawled_skills(files, base_path, redact_url(source.value))
    if source.kind == "plugin":
        return group_crawled_plugins(files, redact_url(source.value))
    return group_crawled_agents(files, redact_url(source.value))


def looks_like_html(data: bytes, content_type: str) -> bool:
    if "html" in content_type.lower():
        return True
    prefix = data.lstrip()[:128].lower()
    return prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html") or b"<a " in prefix


def resolve_json_collection(source: SourceSpec, data: bytes, base_url: str, client: HttpClient) -> list[Artifact]:
    safe_base_url = redact_url(base_url)
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"remote JSON collection is invalid: {safe_base_url}") from exc
    if source.kind != "skill" or not isinstance(manifest, dict) or not isinstance(manifest.get("skills"), list):
        raise InstallError(f"JSON collections are supported for skills only: {safe_base_url}")
    artifacts: list[Artifact] = []
    if len(manifest["skills"]) > MAX_COLLECTION_ITEMS:
        raise InstallError(f"remote skill collection exceeds {MAX_COLLECTION_ITEMS} items: {safe_base_url}")
    collection_base = urljoin(base_url, "./")
    for entry in manifest["skills"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not isinstance(entry.get("files"), list):
            raise InstallError(f"invalid skill entry in remote manifest: {safe_base_url}")
        name = entry["name"]
        validate_name(name, safe_base_url)
        if "SKILL.md" not in entry["files"] or not all(isinstance(item, str) for item in entry["files"]):
            raise InstallError(f"remote skill manifest entry lacks a valid SKILL.md: {name}")
        files: dict[str, bytes] = {}
        if len(entry["files"]) > MAX_FILES:
            raise InstallError(f"remote skill contains more than {MAX_FILES} files: {name}")
        safe_files = [validate_relative_path(relative, f"remote manifest skill {name}") for relative in entry["files"]]
        if len(set(safe_files)) != len(safe_files):
            raise InstallError(f"remote skill manifest contains duplicate files: {name}")
        total_entry_bytes = 0
        for safe_relative in safe_files:
            file_url = urljoin(collection_base, f"{name}/{safe_relative}")
            file_parts = urlsplit(file_url)
            base_parts = urlsplit(collection_base)
            if (file_parts.scheme, file_parts.netloc) != (base_parts.scheme, base_parts.netloc):
                raise InstallError(f"remote manifest points outside its origin: {redact_url(file_url)}")
            file_data, final_file_url, _ = client.get(file_url)
            final_parts = urlsplit(final_file_url)
            if (final_parts.scheme, final_parts.netloc) != (base_parts.scheme, base_parts.netloc):
                raise InstallError(f"remote manifest redirected outside its origin: {redact_url(file_url)}")
            if len(file_data) > MAX_FILE_BYTES or total_entry_bytes + len(file_data) > MAX_TOTAL_BYTES:
                raise InstallError(f"remote skill exceeds its file-size limit: {redact_url(file_url)}")
            total_entry_bytes += len(file_data)
            files[safe_relative] = file_data
        artifacts.append(make_skill_artifact(files, name, safe_base_url, False, None))
    if not artifacts:
        raise InstallError(f"remote skill manifest is empty: {safe_base_url}")
    return artifacts


def group_crawled_skills(files: dict[str, bytes], base_path: str, origin: str) -> list[Artifact]:
    base_root = base_path.rstrip("/") or "/"
    direct_skill_url = f"{base_root}/SKILL.md" if base_root != "/" else "/SKILL.md"
    paths = {unquote(urlsplit(url).path): (url, data) for url, data in files.items()}
    if direct_skill_url in paths:
        root_path = base_root
        prefix = root_path.rstrip("/") + "/" if root_path != "/" else "/"
        grouped = {}
        for path, (_, data) in paths.items():
            if path.startswith(prefix):
                relative = path[len(prefix) :]
                if relative:
                    grouped[validate_relative_path(relative, f"HTML skill root {root_path}")] = data
        return [make_skill_artifact(grouped, PurePosixPath(root_path).name, origin, False, None)]

    roots: set[str] = set()
    prefix = base_root.rstrip("/") + "/" if base_root != "/" else "/"
    for path in paths:
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix) :]
        parts = PurePosixPath(relative).parts
        if len(parts) == 2 and parts[1] == "SKILL.md":
            roots.add(f"{base_root.rstrip('/')}/{parts[0]}")
    artifacts: list[Artifact] = []
    for root_path in sorted(roots):
        root_prefix = root_path.rstrip("/") + "/"
        grouped = {}
        for path, (_, data) in paths.items():
            if path.startswith(root_prefix):
                relative = path[len(root_prefix) :]
                if relative:
                    grouped[validate_relative_path(relative, f"HTML skill root {root_path}")] = data
        artifacts.append(make_skill_artifact(grouped, PurePosixPath(root_path).name, origin, False, None))
    if not artifacts:
        raise InstallError(f"HTML source contains no direct skill directories: {origin}")
    if len(artifacts) > MAX_COLLECTION_ITEMS:
        raise InstallError(f"HTML skill collection exceeds {MAX_COLLECTION_ITEMS} items: {origin}")
    return artifacts


def group_crawled_plugins(files: dict[str, bytes], origin: str) -> list[Artifact]:
    artifacts = [
        make_plugin_artifact(data, PurePosixPath(unquote(urlsplit(url).path)).name, origin, False, None)
        for url, data in sorted(files.items())
        if PurePosixPath(unquote(urlsplit(url).path)).suffix.lower() == ".js"
    ]
    if not artifacts:
        raise InstallError(f"HTML source contains no JavaScript plugins: {origin}")
    if len(artifacts) > MAX_COLLECTION_ITEMS:
        raise InstallError(f"HTML plugin collection exceeds {MAX_COLLECTION_ITEMS} items: {origin}")
    return artifacts


def group_crawled_agents(files: dict[str, bytes], origin: str) -> list[Artifact]:
    artifacts: list[Artifact] = []
    for url, data in sorted(files.items()):
        path = PurePosixPath(unquote(urlsplit(url).path))
        if path.suffix.lower() != ".md":
            continue
        try:
            artifacts.append(make_agent_artifact(data, path.name, origin, False, None))
        except InstallError:
            continue
    if not artifacts:
        raise InstallError(f"HTML source contains no valid agent Markdown files: {origin}")
    return artifacts


def resolve_remote_source(source: SourceSpec, client: HttpClient) -> list[Artifact]:
    client.start_source()
    if source.kind == "plugin":
        print(
            f"warning: remote plugin code will execute when OpenCode starts: {redact_url(source.value)}",
            file=sys.stderr,
        )
    github = parse_github_url(source.value)
    if github:
        return resolve_github_source(source, github, client)
    return resolve_generic_http(source, source.value, client)


def artifact_key(artifact: Artifact) -> tuple[str, str]:
    name = artifact.name
    if artifact.kind == "plugin":
        name = unicodedata.normalize("NFC", name).casefold()
    return artifact.kind, name


def resolve_sources(sources: list[SourceSpec]) -> tuple[list[Artifact], list[str], list[str]]:
    client = HttpClient()
    selected: dict[tuple[str, str], Artifact] = {}
    duplicates: list[str] = []
    skipped: list[str] = []
    for source in sources:
        artifacts = resolve_local_source(source) if not is_url(source.value) else resolve_remote_source(source, client)
        for artifact in artifacts:
            key = artifact_key(artifact)
            if not artifact.content_hash:
                artifact.content_hash = artifact.calculate_hash()
            current = selected.get(key)
            if current is None:
                selected[key] = artifact
                continue
            if current.content_hash == artifact.content_hash:
                duplicates.append(f"deduplicated {artifact.kind} {artifact.name} from {artifact.display_source}")
                continue
            if current.bundled:
                skipped.append(
                    f"bundled {artifact.kind} {artifact.name} wins over conflicting external source {artifact.origin}"
                )
                continue
            if artifact.bundled:
                selected[key] = artifact
                skipped.append(f"bundled {artifact.kind} {artifact.name} replaced an external source")
                continue
            raise InstallError(
                f"conflicting external {artifact.kind} named {artifact.name}: "
                f"{current.origin} and {artifact.origin}"
            )
    return list(selected.values()), duplicates, skipped


def ensure_not_inside(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        return
    raise InstallError(f"{label} cannot be inside the installation target: {path}")


def validated_artifact_files(artifact: Artifact) -> dict[str, bytes]:
    validated: dict[str, bytes] = {}
    collision_keys: set[str] = set()
    for relative, data in artifact.files.items():
        safe_relative = validate_relative_path(relative, f"artifact {artifact.name}")
        collision_key = "/".join(part.casefold().rstrip(". ") for part in safe_relative.split("/"))
        if collision_key in collision_keys:
            raise InstallError(f"artifact contains colliding paths: {artifact.name}")
        collision_keys.add(collision_key)
        validated[safe_relative] = data
    return validated


def cache_artifact(artifact: Artifact, cache_root: Path, dry_run: bool) -> Path:
    validated_files = validated_artifact_files(artifact)
    artifact.files = validated_files
    cache_path = cache_root / artifact.content_hash
    if artifact.link_source is not None:
        return artifact.link_source.resolve()
    if cache_root.is_symlink() or (cache_root.exists() and not cache_root.is_dir()):
        raise InstallError(f"cache root is not a real directory: {cache_root}")
    if cache_path.is_symlink():
        raise InstallError(f"cache entry must not be a symlink: {cache_path}")
    if cache_path.exists():
        verify_cached_artifact(cache_path, artifact)
        return cache_path
    if dry_run:
        return cache_path
    cache_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{artifact.content_hash}.", dir=str(cache_root)))
    try:
        for relative, data in artifact.files.items():
            safe_relative = validate_relative_path(relative, f"artifact {artifact.name}")
            destination = temporary.joinpath(*safe_relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o644)
        manifest = {"hash": artifact.content_hash, "kind": artifact.kind, "name": artifact.name}
        (temporary / ".install-manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        try:
            os.replace(temporary, cache_path)
        except FileExistsError:
            shutil.rmtree(temporary)
            verify_cached_artifact(cache_path, artifact)
        else:
            verify_cached_artifact(cache_path, artifact)
    except OSError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return cache_path


def reject_symlink_components(path: Path, stop: Path, context: str) -> None:
    current = path
    stop = stop.resolve(strict=False)
    while True:
        if current.is_symlink():
            raise InstallError(f"{context} contains a symlinked path component: {current}")
        if current.resolve(strict=False) == stop:
            return
        parent = current.parent
        if parent == current:
            raise InstallError(f"{context} is outside its expected root: {path}")
        current = parent


def verify_cached_artifact(cache_path: Path, artifact: Artifact) -> None:
    if not cache_path.is_dir() or cache_path.is_symlink():
        raise InstallError(f"cache entry is not a directory: {cache_path}")
    reject_symlink_components(cache_path, cache_path.parent, "cache entry")
    manifest_path = cache_path / ".install-manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise InstallError(f"cache entry has no manifest: {cache_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cache manifest is invalid: {manifest_path}") from exc
    if manifest != {"hash": artifact.content_hash, "kind": artifact.kind, "name": artifact.name}:
        raise InstallError(f"cache manifest does not match source: {cache_path}")
    expected_paths = {".install-manifest.json"}
    expected_directories: set[str] = set()
    for relative, expected in artifact.files.items():
        safe_relative = validate_relative_path(relative, f"artifact {artifact.name}")
        path = cache_path.joinpath(*safe_relative.split("/"))
        reject_symlink_components(path, cache_path, "cache entry")
        if not path.is_file() or path.is_symlink() or path.read_bytes() != expected:
            raise InstallError(f"cache content does not match source {artifact.origin}: {path}")
        expected_paths.add(safe_relative)
        parts = safe_relative.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            expected_directories.add("/".join(parts[:index]))
    actual_paths: set[str] = set()
    for path in cache_path.rglob("*"):
        relative = path.relative_to(cache_path).as_posix()
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise InstallError(f"cache entry contains an unsupported filesystem entry: {path}")
        if path.is_dir():
            if relative not in expected_directories:
                raise InstallError(f"cache entry contains an unexpected directory: {path}")
        else:
            actual_paths.add(relative)
    if actual_paths != expected_paths:
        raise InstallError(f"cache entry contains unexpected files: {cache_path}")


def prepare_plan(
    artifacts: list[Artifact],
    target: Path,
    dry_run: bool,
    provider_overlays: dict[str, dict[str, object]],
    model_aliases: dict[str, object],
    agent_models: dict[str, object],
    selected_profile: Optional[str],
) -> InstallPlan:
    resolved_agent_routing = resolve_agent_routing(artifacts, model_aliases, agent_models)
    runtime_config = merge_provider_config(
        target,
        provider_overlays,
        resolved_agent_routing,
    )
    cache_root = target / ".install-cache"
    backup_root = target / ".install-backups" / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    state_path = target / ".install-state.json"
    agents_dir = target / "agents"
    skills_dir = target / "skills"
    plugins_dir = target / "plugins"
    if target.is_symlink():
        raise InstallError(f"installation target must not be a symlink: {target}")
    if target.exists() and not target.is_dir():
        raise InstallError(f"installation target is not a directory: {target}")
    for directory in (agents_dir, skills_dir, plugins_dir):
        if directory.exists() and not directory.is_dir():
            raise InstallError(f"installation directory is not a directory: {directory}")
        if directory.is_symlink():
            raise InstallError(f"installation directory must not be a symlink: {directory}")
    backup_parent = target / ".install-backups"
    if backup_parent.is_symlink() or (backup_parent.exists() and not backup_parent.is_dir()):
        raise InstallError(f"backup directory must be a real directory: {backup_parent}")
    if backup_root.is_symlink() or (backup_root.exists() and not backup_root.is_dir()):
        raise InstallError(f"backup target must be a real directory: {backup_root}")
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)
        agents_dir.mkdir(exist_ok=True)
        skills_dir.mkdir(exist_ok=True)
        plugins_dir.mkdir(exist_ok=True)
        backup_parent.mkdir(exist_ok=True)
    for artifact in artifacts:
        if artifact.link_source is not None:
            ensure_not_inside(artifact.link_source.resolve(), target, f"local {artifact.kind} source")
        artifact.cache_path = cache_artifact(artifact, cache_root, dry_run)
    return InstallPlan(
        artifacts,
        target,
        backup_root,
        cache_root,
        state_path,
        runtime_config,
        sorted(provider_overlays),
        resolved_agent_routing,
        selected_profile,
    )


def expected_link_target(artifact: Artifact) -> Path:
    if artifact.cache_path is None:
        raise InstallError(f"artifact was not prepared: {artifact.name}")
    if artifact.kind in {"agent", "plugin"} and artifact.link_source is None:
        return artifact.cache_path / next(iter(artifact.files))
    return artifact.cache_path


def destination_for(artifact: Artifact, target: Path) -> Path:
    if artifact.kind == "agent":
        return target / "agents" / f"{artifact.name}.md"
    if artifact.kind == "skill":
        return target / "skills" / artifact.name
    if artifact.kind == "plugin":
        return target / "plugins" / artifact.name
    raise InstallError(f"unknown artifact kind: {artifact.kind}")


def same_link(destination: Path, expected: Path) -> bool:
    if not destination.is_symlink():
        return False
    try:
        return destination.resolve(strict=False) == expected.resolve(strict=False)
    except OSError:
        return False


def create_relative_link(destination: Path, expected: Path) -> None:
    temporary = destination.parent / f".{destination.name}.install-{uuid.uuid4().hex}"
    try:
        relative_target = os.path.relpath(expected, destination.parent)
        temporary.symlink_to(relative_target, target_is_directory=expected.is_dir())
        os.replace(temporary, destination)
    except (OSError, ValueError):
        temporary.unlink(missing_ok=True)
        raise


def backup_or_unlink(
    destination: Path, backup_path: Path, directory_hint: bool = False
) -> tuple[Optional[Path], Optional[str], bool]:
    if not destination.exists() and not destination.is_symlink():
        return None, None, False
    if destination.is_symlink():
        original_target = os.readlink(destination)
        attributes = getattr(os.lstat(destination), "st_file_attributes", 0)
        original_is_directory = destination.is_dir() or bool(attributes & 0x10) or directory_hint
        destination.unlink()
        return None, original_target, original_is_directory
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(destination), str(backup_path))
    return backup_path, None, False


def rollback_operations(operations: list[ApplyOperation]) -> None:
    for operation in reversed(operations):
        try:
            if operation.created_link and operation.destination.is_symlink():
                operation.destination.unlink()
            if operation.backup and operation.backup.exists() and not operation.destination.exists():
                operation.destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(operation.backup), str(operation.destination))
            elif operation.original_symlink is not None and not operation.destination.exists():
                operation.destination.symlink_to(
                    operation.original_symlink,
                    target_is_directory=operation.original_symlink_is_directory,
                )
        except (OSError, ValueError) as exc:
            print(f"warning: rollback could not restore {operation.destination}: {exc}", file=sys.stderr)


def runtime_config_hash(config: dict[str, object]) -> str:
    serialized = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def write_secure_bytes(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    file_descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            file_descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if file_descriptor != -1:
            os.close(file_descriptor)


def copy_secure_file(source: Path, destination: Path) -> None:
    write_secure_bytes(destination, source.read_bytes(), 0o600)


def replace_runtime_config(plan: InstallPlan) -> Optional[ConfigOperation]:
    if plan.runtime_config is None:
        return None
    config_path = plan.target / "opencode.json"
    if config_path.is_symlink():
        raise InstallError(f"runtime OpenCode config must not be a symlink: {config_path}")
    backup: Optional[Path] = None
    created = not config_path.exists()
    mode = stat.S_IMODE(config_path.stat().st_mode) if not created else 0o600
    temporary_mode = 0o600
    temporary = config_path.with_name(f".{config_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        if not created:
            backup = plan.backup_root / "opencode.json"
            backup.parent.mkdir(parents=True, exist_ok=True)
            copy_secure_file(config_path, backup)
            os.chmod(backup, mode)
        write_secure_bytes(
            temporary,
            (json.dumps(plan.runtime_config, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            temporary_mode,
        )
        os.chmod(temporary, mode)
        os.replace(temporary, config_path)
        return ConfigOperation(config_path, backup, created)
    except (OSError, ValueError):
        temporary.unlink(missing_ok=True)
        if backup and backup.exists():
            backup.unlink(missing_ok=True)
        raise


def rollback_runtime_config(operation: Optional[ConfigOperation]) -> None:
    if operation is None:
        return
    try:
        if operation.path.exists() or operation.path.is_symlink():
            operation.path.unlink()
        if operation.backup and operation.backup.exists():
            operation.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(operation.backup), str(operation.path))
    except OSError as exc:
        print(f"warning: rollback could not restore {operation.path}: {exc}", file=sys.stderr)


def write_state(plan: InstallPlan) -> None:
    state = {
        "version": 1,
        "target": str(plan.target),
        "installed": [
            {
                "kind": artifact.kind,
                "name": artifact.name,
                "source": artifact.display_source,
                "hash": artifact.content_hash,
                "destination": str(destination_for(artifact, plan.target)),
                "link_target": str(expected_link_target(artifact)),
            }
            for artifact in sorted(plan.artifacts, key=lambda item: (item.kind, item.name))
        ],
        "provider_overlay": {
            "providers": plan.provider_names,
            "config_hash": runtime_config_hash(plan.runtime_config) if plan.runtime_config is not None else None,
        },
        "agent_routing": {
            "profile": plan.selected_profile,
            "assignments": plan.agent_routing,
        },
    }
    temporary = plan.state_path.with_name(f".{plan.state_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, plan.state_path)


def apply_plan(plan: InstallPlan, dry_run: bool) -> None:
    if dry_run:
        return
    operations: list[ApplyOperation] = []
    config_operation: Optional[ConfigOperation] = None
    try:
        for artifact in sorted(plan.artifacts, key=lambda item: (item.kind, item.name)):
            destination = destination_for(artifact, plan.target)
            expected = expected_link_target(artifact)
            if same_link(destination, expected):
                print(f"unchanged {destination}")
                continue
            backup_directory = {
                "agent": "agents",
                "skill": "skills",
                "plugin": "plugins",
            }.get(artifact.kind)
            if backup_directory is None:
                raise InstallError(f"unknown artifact kind: {artifact.kind}")
            backup_path = plan.backup_root / backup_directory / destination.name
            backup, original_symlink, original_is_directory = backup_or_unlink(
                destination, backup_path, artifact.kind == "skill"
            )
            operation = ApplyOperation(destination, expected, backup, original_symlink, original_is_directory)
            operations.append(operation)
            create_relative_link(destination, expected)
            operation.created_link = True
            if backup:
                print(f"backed up {destination} -> {backup}")
            elif original_symlink is not None:
                print(f"replaced symlink {destination}")
            else:
                print(f"installed {destination}")
        config_operation = replace_runtime_config(plan)
        write_state(plan)
    except (OSError, ValueError, InstallError) as exc:
        rollback_runtime_config(config_operation)
        rollback_operations(operations)
        raise InstallError(f"installation failed and was rolled back: {exc}") from exc


def print_plan(plan: InstallPlan, dry_run: bool) -> None:
    action = "would install" if dry_run else "installing"
    print(f"{action} {len(plan.artifacts)} artifacts into {plan.target}")
    for artifact in sorted(plan.artifacts, key=lambda item: (item.kind, item.name)):
        destination = destination_for(artifact, plan.target)
        expected = expected_link_target(artifact)
        print(f"  {artifact.kind}: {artifact.name} <- {artifact.display_source} [{artifact.content_hash[:12]}]")
        if dry_run:
            print(f"    link: {destination} -> {expected}")
    if plan.provider_names:
        print(f"  opencode providers: {', '.join(plan.provider_names)}")
    if plan.selected_profile:
        print(f"  agent-models profile: {plan.selected_profile}")
    if plan.agent_routing:
        print("  agent models:")
        for agent_name, routing in sorted(plan.agent_routing.items()):
            variant = f" ({routing['variant']})" if "variant" in routing else ""
            print(f"    {agent_name}: {routing['model']}{variant}")
    for message in plan.duplicate_messages:
        print(f"  {message}")
    for message in plan.skipped_messages:
        print(f"  {message}")


def run(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        target = resolve_target(args.target)
        sources, provider_overlays, model_aliases, agent_profiles = build_sources(args)
        if args.profile is not None:
            if args.profile not in agent_profiles:
                available = ", ".join(sorted(agent_profiles)) or "(none)"
                raise InstallError(f"unknown profile {args.profile!r}; available profiles: {available}")
            selected_profile = args.profile
            agent_models = agent_profiles[args.profile]
        else:
            selected_profile = None
            agent_models = {}
        artifacts, duplicates, skipped = resolve_sources(sources)
        plan = prepare_plan(
            artifacts,
            target,
            args.dry_run,
            provider_overlays,
            model_aliases,
            agent_models,
            selected_profile,
        )
        plan.duplicate_messages.extend(duplicates)
        plan.skipped_messages.extend(skipped)
        print_plan(plan, args.dry_run)
        apply_plan(plan, args.dry_run)
        if not args.dry_run:
            print(f"installed {len(plan.artifacts)} artifacts")
        return 0
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: filesystem operation failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: invalid input: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
