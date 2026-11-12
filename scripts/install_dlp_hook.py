"""Corporate DLP pre-commit hook installer and staged index validator for Project Poopdeck."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

_SCRIPT_PATH = Path(__file__).resolve()
_CANDIDATE_ROOT = _SCRIPT_PATH.parent.parent
if (_CANDIDATE_ROOT / "poopdeck" / "config.py").is_file():
    if str(_CANDIDATE_ROOT) not in sys.path:
        sys.path.insert(0, str(_CANDIDATE_ROOT))
else:
    _env_engine = os.environ.get("POOPDECK_ENGINE_ROOT", "").strip()
    if _env_engine and (Path(_env_engine) / "poopdeck" / "config.py").is_file():
        if _env_engine not in sys.path:
            sys.path.insert(0, _env_engine)

import poopdeck.config as _poopdeck_config_mod
from poopdeck.config import PoopdeckConfig, load_config

DLP_BANNER = "[POOPDECK-DLP-BLOCKED]"

_DUMP_FILENAME_TOKENS: tuple[str, ...] = (
    "catalog_dump",
    "table_dump",
    "information_schema_dump",
)

_CRED_PLACEHOLDER_GUARD = (
    r"(?!\[REDACTED:)(?!<)(?!\$\{)(?!\.{2,})(?!YOUR_)(?!REPLACE_)(?!CHANGE_ME)"
)

_CONN_PASSWORD_VALUE = (
    r"(?:"
    + r"\\?\x22"
    + _CRED_PLACEHOLDER_GUARD
    + r"\{?+"
    + _CRED_PLACEHOLDER_GUARD
    + r"(?=[^\x22\r\nA-Za-z0-9]*+[A-Za-z0-9])[^\x22\r\n]{4,}+"
    + r"|"
    + r"\\?\x27"
    + _CRED_PLACEHOLDER_GUARD
    + r"\{?+"
    + _CRED_PLACEHOLDER_GUARD
    + r"(?=[^\x27\r\nA-Za-z0-9]*+[A-Za-z0-9])[^\x27\r\n]{4,}+"
    + r"|"
    + r"\{(?![ \t]*+\})"
    + _CRED_PLACEHOLDER_GUARD
    + r"(?=[^}\r\nA-Za-z0-9]*+[A-Za-z0-9])[^}\r\n]{4,}+"
    + r"|"
    + r"(?![\s;\x22\x27`\\{])"
    + _CRED_PLACEHOLDER_GUARD
    + r"(?=[^\s;\x22\x27`\\A-Za-z0-9]*+[A-Za-z0-9])[^\s;\x22\x27`\\]{4,}+"
    + r")"
)

_PEM_HEADER_PATTERN = (
    r"-----BEGIN "
    + r"(?:[A-Z0-9 _]|-(?!-)){0,64}PRIVATE KEY-----"
)

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Databricks PAT token",
        re.compile(r"\bdapi[0-9a-fA-F]{20,64}\b"),
    ),
    (
        "GitHub personal access token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,255}\b"),
    ),
    (
        "GitHub fine-grained PAT",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b"),
    ),
    (
        "Anthropic API key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,255}\b"),
    ),
    (
        "AWS access key ID",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        "Azure Storage AccountKey",
        re.compile(
            r"(?i)\bAccountKey[ \t]*+=[ \t]*+(?:\\?[\x22\x27])?+"
            + _CRED_PLACEHOLDER_GUARD
            + r"[A-Za-z0-9+/]{16,}+={0,3}+"
        ),
    ),
    (
        "Databricks JDBC password",
        re.compile(
            r"(?i)jdbc:databricks://(?:[^\r\njp]++|j(?!dbc:databricks://)[A-Za-ik-z0-9_]*+|p[A-Za-ik-z0-9_]*+)*?"
            + r"\bpwd[ \t]*+=[ \t]*+"
            + _CONN_PASSWORD_VALUE
        ),
    ),
    (
        "SQL Server TCP connection password",
        re.compile(
            r"(?i)\bserver[ \t]*+=[ \t]*+tcp:(?:[^\r\nsp]++|s(?!erver[ \t]*+=[ \t]*+tcp:)[A-Za-z0-9_]*+|p[A-Za-z0-9_]*+)*?"
            + r"\b(?:password|pwd)[ \t]*+=[ \t]*+"
            + _CONN_PASSWORD_VALUE
        ),
    ),
    (
        "Bearer JWT token",
        re.compile(
            r"(?i)\bBearer\s++eyJ[A-Za-z0-9_-]{4,}+\.[A-Za-z0-9_-]{4,}+(?:\.[A-Za-z0-9_-]++)?+"
        ),
    ),
    (
        "PEM private key block",
        re.compile(_PEM_HEADER_PATTERN),
    ),
)

_SQL_IDENT_PART = (
    r"(?:\[[^\]\r\n;]{1,256}+\]|`[^`\r\n;]{1,256}+`|\x22[^\x22\r\n;]{1,256}+\x22|[A-Za-z0-9_$#@]++)"
)
_SQL_TABLE_IDENT = rf"{_SQL_IDENT_PART}(?:\.{_SQL_IDENT_PART})*+"
_SQL_COL_LIST = r"\((?:[^();\[\]]++|\[[^\];\r\n]*+\])*+\)"
_SQL_INSERT_HEADER = (
    r"\bINSERT\s++INTO\s++"
    r"(?:TABLE\b(?!\s*+(?:\(|VALUES\b))\s++)?+"
    rf"{_SQL_TABLE_IDENT}\s*+"
    rf"(?:PARTITION\s*+{_SQL_COL_LIST}\s*+)?+"
    rf"(?:{_SQL_COL_LIST}\s*+)?+"
    r"\bVALUES\s*+\("
)
_SQL_SQ = r"\x27(?:[^\x27\\]++|\\.|'')*+\x27"
_SQL_DQ = r"\x22(?:[^\x22\\]++|\\.|\"\")*+\x22"
_SQL_NL = r"(?>\r\n|\r|\n)(?![ \t]*+(?:\r\n|\r|\n))"
_SQL_ATOM_BASE = rf"[^();\x27\x22`\r\n]++|{_SQL_NL}|{_SQL_SQ}|{_SQL_DQ}"
_SQL_PAREN_D2 = rf"\((?:{_SQL_ATOM_BASE})*+\)"
_SQL_PAREN_D1 = rf"\((?:{_SQL_ATOM_BASE}|{_SQL_PAREN_D2})*+\)"
_SQL_FIRST_TUPLE_BODY = rf"(?:{_SQL_ATOM_BASE}|{_SQL_PAREN_D1})*+"

_SQL_INSERT_STMT_RE = re.compile(rf"(?is){_SQL_INSERT_HEADER}")
_SQL_MULTIROW_INSERT_RE = re.compile(
    rf"(?is){_SQL_INSERT_HEADER}{_SQL_FIRST_TUPLE_BODY}\)\s*+,\s*+\("
)


def _sh_single_quote(value: str) -> str:
    """Escape a string safely for single-quoted POSIX /bin/sh assignment."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _resolve_git_dir(repo_root: Path) -> Path:
    """Resolve per-worktree .git directory (or gitfile pointer) for repo_root."""
    repo_path = Path(repo_root)
    dot_git = repo_path / ".git"
    if not dot_git.exists():
        raise FileNotFoundError(
            f"Git repository metadata (.git) not found under: {repo_path}"
        )
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        content = dot_git.read_text(encoding="utf-8-sig").strip()
        if content.lower().startswith("gitdir:"):
            raw_target = content.split(":", 1)[1].strip()
            target = Path(raw_target)
            if not target.is_absolute():
                target = repo_path / target
            if target.exists():
                return target
    raise FileNotFoundError(f"Invalid .git entry at: {dot_git}")


def _resolve_common_git_dir(git_dir: Path) -> Path:
    """Resolve shared common Git directory (follows 'commondir' in linked worktrees)."""
    commondir_file = git_dir / "commondir"
    if commondir_file.is_file():
        raw_common = commondir_file.read_text(encoding="utf-8-sig").strip()
        if raw_common:
            common_target = Path(raw_common)
            if not common_target.is_absolute():
                common_target = git_dir / common_target
            resolved_common = common_target.resolve()
            if resolved_common.is_dir():
                return resolved_common
            raise FileNotFoundError(
                f"Invalid Git commondir target at: {commondir_file}"
            )
    return git_dir


def _resolve_hooks_dir(repo_root: Path) -> Path:
    """Resolve active Git hooks directory (shared commondir/hooks for linked worktrees)."""
    git_dir = _resolve_git_dir(repo_root)
    common_git_dir = _resolve_common_git_dir(git_dir)
    return common_git_dir / "hooks"


def install_hook(repo_root: Path) -> Path:
    """Write LF-only Windows sh.exe-safe .git/hooks/pre-commit DLP hook."""
    repo_path = Path(repo_root)
    hook_path = _resolve_hooks_dir(repo_path) / "pre-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)

    posix_py = sys.executable.replace("\\", "/")
    engine_root_posix = (
        Path(_poopdeck_config_mod.__file__).resolve().parent.parent.as_posix()
    )

    quoted_py = _sh_single_quote(posix_py)
    quoted_engine = _sh_single_quote(engine_root_posix)

    script_lines = [
        "#!/bin/sh",
        "set -e",
        "",
        'REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"',
        f"POOPDECK_ENGINE_ROOT={quoted_engine}",
        "export POOPDECK_ENGINE_ROOT",
        "",
        'PYTHON_CMD=""',
        f"for candidate in {quoted_py} python python3; do",
        '    if "$candidate" -c "import sys" >/dev/null 2>&1; then',
        '        PYTHON_CMD="$candidate"',
        "        break",
        "    fi",
        "done",
        "",
        'if [ -z "$PYTHON_CMD" ]; then',
        f'    echo "{DLP_BANNER} No working Python interpreter found for pre-commit DLP guard." >&2',
        "    exit 1",
        "fi",
        "",
        'if [ -f "$REPO_ROOT/scripts/install_dlp_hook.py" ]; then',
        '    HOOK_SCRIPT="$REPO_ROOT/scripts/install_dlp_hook.py"',
        'elif [ -f "$POOPDECK_ENGINE_ROOT/scripts/install_dlp_hook.py" ]; then',
        '    HOOK_SCRIPT="$POOPDECK_ENGINE_ROOT/scripts/install_dlp_hook.py"',
        "else",
        f'    echo "{DLP_BANNER} Missing DLP hook script: scripts/install_dlp_hook.py" >&2',
        "    exit 1",
        "fi",
        "",
        'exec "$PYTHON_CMD" "$HOOK_SCRIPT" --hook --repo-root "$REPO_ROOT"',
        "",
    ]
    script_text = "\n".join(script_lines)

    with open(hook_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(script_text)

    try:
        hook_path.chmod(0o755)
    except OSError:
        pass

    return hook_path


def _build_repo_git_env(repo_root: Path) -> dict[str, str]:
    """Strip outer GIT_* env vars while preserving repo-local GIT_INDEX_FILE for partial/-a commits."""
    env = {
        key: val
        for key, val in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        git_dir_resolved = _resolve_git_dir(repo_root).resolve()
    except FileNotFoundError:
        return env

    raw_idx = os.environ.get("GIT_INDEX_FILE", "").strip()
    if raw_idx:
        idx_path = Path(raw_idx)
        if idx_path.is_absolute():
            candidates = [idx_path]
        else:
            candidates = [
                Path(repo_root) / idx_path,
                git_dir_resolved / idx_path,
                Path.cwd() / idx_path,
            ]
        for cand in candidates:
            try:
                resolved_cand = cand.resolve()
            except OSError:
                continue
            if resolved_cand.is_file() and resolved_cand.is_relative_to(
                git_dir_resolved
            ):
                env["GIT_INDEX_FILE"] = str(resolved_cand)
                break

    return env


def _resolve_dlp_config(
    repo_root: Path,
    config_path: Path | str | None = None,
) -> PoopdeckConfig:
    """Load DLP configuration scoped to repo_root without cross-repo cwd leakage."""
    if config_path is not None and str(config_path).strip():
        return load_config(config_path)
    if os.environ.get("POOPDECK_CONFIG_PATH", "").strip():
        return load_config()
    for filename in ("config.yaml", "config.example.yaml"):
        candidate = Path(repo_root) / filename
        if candidate.is_file():
            return load_config(candidate)
    if Path(repo_root).resolve() == Path.cwd().resolve():
        return load_config()
    return PoopdeckConfig()


_SCHEME_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SCP_REMOTE_RE = re.compile(
    r"^(?:[^@/\s:?#]+@)?(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):([^\s?#]+)(?:[?#].*)?$"
)
_HOST_PORT_PREFIX_RE = re.compile(r"^[^@/]+:\d+(?:/|$)")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_REGEX_META_CHARS = frozenset("^$\\*+?()[]{}|")
_SSH_HOST_ALIASES: dict[str, str] = {
    "ssh.dev.azure.com": "dev.azure.com",
    "ssh.github.com": "github.com",
    "altssh.gitlab.com": "gitlab.com",
}


class _ParsedRemote(NamedTuple):
    scheme: str
    host: str
    path: str
    sanitized_url: str
    host_paths: tuple[tuple[str, str], ...]


def _normalize_url_path(raw_path: str) -> str | None:
    """Decode percent-escapes and normalize POSIX URL path segments; return None on root escape."""
    decoded = unquote(raw_path).replace("\\", "/")
    segments: list[str] = []
    for seg in decoded.split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if not segments:
                return None
            segments.pop()
        else:
            segments.append(seg)
    return "/".join(segments)


def _strip_git_suffix(path: str) -> str:
    """Strip trailing .git suffix and leading/trailing slashes from a remote path."""
    return re.sub(r"\.git$", "", path, flags=re.IGNORECASE).strip("/")


def _build_host_paths(host: str, norm_path: str) -> tuple[tuple[str, str], ...]:
    """Build (host, repo_path_no_git) candidates including canonical SSH/Azure DevOps forms."""
    clean_path = _strip_git_suffix(norm_path)
    pairs: list[tuple[str, str]] = [(host, clean_path)]

    alias_host = _SSH_HOST_ALIASES.get(host)
    if alias_host:
        alias_path = clean_path
        if host == "ssh.dev.azure.com" and alias_path.lower().startswith("v3/"):
            alias_path = alias_path[3:]
        if (alias_host, alias_path) not in pairs:
            pairs.append((alias_host, alias_path))

    for cur_host, cur_path in list(pairs):
        if cur_host == "dev.azure.com":
            segs = [s for s in cur_path.split("/") if s]
            if len(segs) >= 4 and segs[2].lower() == "_git":
                no_git_seg = f"{segs[0]}/{segs[1]}/{'/'.join(segs[3:])}"
                if (cur_host, no_git_seg) not in pairs:
                    pairs.append((cur_host, no_git_seg))

    return tuple(pairs)


def _parse_remote_url(url: str) -> _ParsedRemote | None:
    """Parse HTTP(S)/SSH/SCP/file Git remote URL, stripping userinfo, port, ?query, and #fragment."""
    raw = url.strip()
    if not raw:
        return None

    if _SCHEME_URL_RE.match(raw):
        try:
            parts = urlsplit(raw)
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        if scheme == "file":
            norm_path = _normalize_url_path(parts.path)
            if norm_path is None:
                return None
            return _ParsedRemote(
                scheme="file",
                host="",
                path=norm_path,
                sanitized_url=f"file:///{norm_path}",
                host_paths=(),
            )
        host = (parts.hostname or "").strip().lower().rstrip(".")
        if not host:
            return None
        norm_path = _normalize_url_path(parts.path)
        if norm_path is None:
            return None
        sanitized = f"{scheme}://{host}/{norm_path}" if norm_path else f"{scheme}://{host}"
        return _ParsedRemote(
            scheme=scheme,
            host=host,
            path=norm_path,
            sanitized_url=sanitized,
            host_paths=_build_host_paths(host, norm_path),
        )

    if (
        raw.startswith(("/", "./", "../", ".\\", "..\\", "\\"))
        or _WINDOWS_DRIVE_RE.match(raw)
    ):
        clean_local = raw.replace("\\", "/")
        return _ParsedRemote(
            scheme="file",
            host="",
            path=clean_local,
            sanitized_url=clean_local,
            host_paths=(),
        )

    if not _HOST_PORT_PREFIX_RE.match(raw):
        scp_match = _SCP_REMOTE_RE.match(raw)
        if scp_match:
            host = scp_match.group(1).strip("[]").lower().rstrip(".")
            norm_path = _normalize_url_path(scp_match.group(2))
            if not host or norm_path is None:
                return None
            sanitized = f"git@{host}:{norm_path}" if norm_path else f"git@{host}"
            return _ParsedRemote(
                scheme="ssh",
                host=host,
                path=norm_path,
                sanitized_url=sanitized,
                host_paths=_build_host_paths(host, norm_path),
            )

    clean_local = raw.replace("\\", "/")
    return _ParsedRemote(
        scheme="file",
        host="",
        path=clean_local,
        sanitized_url=clean_local,
        host_paths=(),
    )


def _unescape_regex_literal(pattern: str) -> str:
    """Unescape regex-escaped punctuation (e.g., '\\.' -> '.') for specificity checks."""
    return re.sub(r"\\([^A-Za-z0-9])", r"\1", pattern)


def _has_regex_syntax(pattern: str) -> bool:
    """Return True if pattern contains regex metacharacters."""
    return any(ch in _REGEX_META_CHARS for ch in pattern)


def _corporate_pattern_matches(pattern: str, parsed: _ParsedRemote) -> bool:
    """Match a corporate allowlist pattern enforcing strict authority and path-segment boundaries."""
    clean = pattern.strip()
    if not clean:
        return False

    if clean.startswith("*.") and not _has_regex_syntax(clean[2:]):
        domain_part = clean[2:]
        if "/" not in domain_part and ":" not in domain_part:
            dom = domain_part.lower().rstrip(".")
            return bool(dom) and any(
                h == dom or h.endswith("." + dom) for h, _ in parsed.host_paths
            )

    unescaped = _unescape_regex_literal(clean)
    is_escaped_literal_only = (
        not _has_regex_syntax(unescaped)
        and not clean.startswith("^")
        and not clean.endswith("$")
    )

    if not _has_regex_syntax(clean) or is_escaped_literal_only:
        lit = unescaped
        if parsed.scheme == "file":
            return False
        is_scp_literal = (
            not _SCHEME_URL_RE.match(lit)
            and ":" in lit
            and not _HOST_PORT_PREFIX_RE.match(lit)
        )
        if _SCHEME_URL_RE.match(lit) or is_scp_literal:
            pat_parsed = _parse_remote_url(lit)
            if pat_parsed is None or not pat_parsed.host:
                return False
            pat_host = pat_parsed.host
            pat_path = _strip_git_suffix(pat_parsed.path).lower()
        else:
            try:
                split_pat = urlsplit("https://" + lit.lstrip("/"))
            except ValueError:
                return False
            pat_host = (split_pat.hostname or "").strip().lower().rstrip(".")
            norm_pat_path = _normalize_url_path(split_pat.path)
            if not pat_host or norm_pat_path is None:
                return False
            pat_path = _strip_git_suffix(norm_pat_path).lower()

        for cand_host, cand_path in parsed.host_paths:
            cand_lower = cand_path.lower()
            if not pat_path:
                if cand_host == pat_host or cand_host.endswith("." + pat_host):
                    return True
            else:
                if cand_host == pat_host and (
                    cand_lower == pat_path or cand_lower.startswith(pat_path + "/")
                ):
                    return True
        return False

    try:
        compiled = re.compile(clean, flags=re.IGNORECASE)
    except re.error:
        return False

    if parsed.scheme == "file":
        if not clean.startswith("^"):
            return False
        m_file = compiled.search(parsed.sanitized_url)
        if m_file is None or m_file.start() != 0:
            return False
        end_f = m_file.end()
        return (
            end_f == len(parsed.sanitized_url)
            or (end_f > 0 and parsed.sanitized_url[end_f - 1] == "/")
            or parsed.sanitized_url[end_f] == "/"
        )

    cand_spans: list[tuple[str, int, int]] = []
    if _SCHEME_URL_RE.match(parsed.sanitized_url):
        h_off = len(parsed.scheme) + 3
        h_end = h_off + len(parsed.host)
        cand_spans.append((parsed.sanitized_url, h_off, h_end))
        no_git_san = re.sub(r"\.git$", "", parsed.sanitized_url, flags=re.IGNORECASE)
        if no_git_san != parsed.sanitized_url:
            cand_spans.append((no_git_san, h_off, h_end))
    elif parsed.sanitized_url.startswith("git@"):
        h_off = 4
        h_end = h_off + len(parsed.host)
        cand_spans.append((parsed.sanitized_url, h_off, h_end))
        no_git_san = re.sub(r"\.git$", "", parsed.sanitized_url, flags=re.IGNORECASE)
        if no_git_san != parsed.sanitized_url:
            cand_spans.append((no_git_san, h_off, h_end))

    for h, p in parsed.host_paths:
        hp = f"{h}/{p}" if p else h
        cand_spans.append((hp, 0, len(h)))

    for cand, h_off, h_end in cand_spans:
        m = compiled.search(cand)
        if m is None:
            continue
        start, end = m.start(), m.end()
        valid_start = start <= h_off or (
            h_off < start < h_end
            and (cand[start] == "." or cand[start - 1] == ".")
        )
        valid_end = end >= h_end and (
            end == len(cand)
            or (end > 0 and cand[end - 1] in ("/", ":"))
            or cand[end] in ("/", ":")
        )
        if valid_start and valid_end:
            return True
    return False


def _pattern_matches(pattern: str, url: str) -> bool:
    """Match sanitized remote URL against an external/general pattern (substring or regex)."""
    clean = pattern.strip()
    if not clean:
        return False
    parsed = _parse_remote_url(url)
    if parsed is None:
        return False

    targets = [parsed.sanitized_url]
    for h, p in parsed.host_paths:
        hp = f"{h}/{p}" if p else h
        if hp not in targets:
            targets.append(hp)

    low_clean = clean.lower()
    if not _has_regex_syntax(clean):
        return any(low_clean in t.lower() for t in targets)

    try:
        compiled = re.compile(clean, flags=re.IGNORECASE)
    except re.error:
        return False
    return any(compiled.search(t) is not None for t in targets)


def _is_corporate_remote(url: str, cfg: PoopdeckConfig) -> bool:
    """Return True iff url matches an explicit corporate pattern and no broader un-overridden external pattern."""
    parsed = _parse_remote_url(url)
    if parsed is None:
        return False

    matched_corp = [
        pat.strip()
        for pat in cfg.dlp.corporate_remote_patterns
        if _corporate_pattern_matches(pat, parsed)
    ]
    if not matched_corp:
        return False

    norm_matched_corp = [_unescape_regex_literal(corp).lower() for corp in matched_corp]

    for ext_pat in cfg.dlp.external_remote_patterns:
        if _pattern_matches(ext_pat, url):
            ext_norm = _unescape_regex_literal(ext_pat.strip()).lower()
            if not any(ext_norm in corp_norm for corp_norm in norm_matched_corp):
                return False

    return True


def _list_remote_urls(repo_root: Path, git_env: dict[str, str]) -> list[str]:
    """Return deduplicated remote URLs configured on repo_root."""
    res = subprocess.run(
        ["git", "remote", "-v"],
        cwd=str(repo_root),
        env=git_env,
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git remote -v failed in {repo_root}: {err}")

    urls: list[str] = []
    seen: set[str] = set()
    stdout_text = res.stdout.decode("utf-8", errors="replace")
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        rest = parts[1] if len(parts) == 2 else (line.split(None, 1)[1] if len(line.split(None, 1)) == 2 else "")
        cleaned = re.sub(r"\s+\((?:fetch|push)\)\s*$", "", rest).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)
    return urls


def _iter_staged_entries(
    repo_root: Path,
    git_env: dict[str, str],
) -> list[tuple[str, str, str]]:
    """Return (dst_mode, dst_sha, raw_path) tuples for non-deleted staged entries."""
    res = subprocess.run(
        ["git", "diff", "--cached", "--raw", "--no-abbrev", "-z", "--no-renames"],
        cwd=str(repo_root),
        env=git_env,
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git diff --cached failed in {repo_root}: {err}")

    if not res.stdout:
        return []

    chunks = res.stdout.split(b"\x00")
    if chunks and chunks[-1] == b"":
        chunks.pop()

    entries: list[tuple[str, str, str]] = []
    idx = 0
    while idx + 1 < len(chunks):
        meta = chunks[idx].decode("utf-8", errors="replace")
        raw_path = chunks[idx + 1].decode("utf-8", errors="replace")
        idx += 2

        fields = meta.lstrip(":").split()
        if len(fields) < 5:
            continue
        _src_mode, dst_mode, _src_sha, dst_sha, status = fields[:5]
        if status.startswith("D") or dst_mode == "000000":
            continue
        entries.append((dst_mode, dst_sha, raw_path))
    return entries


def _decode_staged_bytes(raw: bytes) -> str:
    """Decode staged blob bytes preserving both UTF-8/ASCII-in-binary and UTF-16/32 text."""
    utf8_text = raw.decode("utf-8", errors="replace")
    if not raw:
        return utf8_text

    parts: list[str] = [utf8_text]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        parts.append(raw.decode("utf-16", errors="replace"))
        if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            parts.append(raw.decode("utf-32", errors="replace"))
    elif b"\x00" in raw[:256] and len(raw) >= 4:
        sample = raw[:512]
        even_bytes = sample[0::2]
        odd_bytes = sample[1::2]
        if odd_bytes and even_bytes:
            odd_zero_ratio = odd_bytes.count(0) / len(odd_bytes)
            even_zero_ratio = even_bytes.count(0) / len(even_bytes)
            if odd_zero_ratio >= 0.60 and even_zero_ratio <= 0.10:
                parts.append(raw.decode("utf-16-le", errors="replace"))
            elif even_zero_ratio >= 0.60 and odd_zero_ratio <= 0.10:
                parts.append(raw.decode("utf-16-be", errors="replace"))
    return "\n".join(parts)


def _read_staged_blob(
    repo_root: Path,
    git_env: dict[str, str],
    blob_sha: str,
) -> str:
    """Read staged blob bytes via git cat-file blob and decode safely."""
    res = subprocess.run(
        ["git", "cat-file", "blob", blob_sha],
        cwd=str(repo_root),
        env=git_env,
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git cat-file blob {blob_sha} failed: {err}")
    return _decode_staged_bytes(res.stdout)


def run_dlp_check(
    repo_root: Path,
    config: PoopdeckConfig | None = None,
) -> tuple[bool, list[str]]:
    """Inspect remotes and staged index; return (allowed, violation_reasons)."""
    repo_path = Path(repo_root)
    try:
        _resolve_git_dir(repo_path)
    except FileNotFoundError as exc:
        return False, [str(exc)]

    cfg = config if config is not None else _resolve_dlp_config(repo_path)
    git_env = _build_repo_git_env(repo_path)

    try:
        remote_urls = _list_remote_urls(repo_path, git_env)
        staged_entries = _iter_staged_entries(repo_path, git_env)
    except RuntimeError as exc:
        return False, [str(exc)]

    if not staged_entries:
        return True, []

    non_corp_urls = [u for u in remote_urls if not _is_corporate_remote(u, cfg)]
    all_remotes_corporate = bool(remote_urls) and not non_corp_urls
    strict_non_corporate = not all_remotes_corporate

    if not remote_urls:
        remote_desc = "no corporate Git remote configured"
    elif non_corp_urls:
        remote_desc = f"external/non-corporate remote(s): {', '.join(non_corp_urls)}"
    else:
        remote_desc = "corporate remote allowlist active"

    reasons: list[str] = []

    for dst_mode, dst_sha, raw_path in staged_entries:
        posix_path = raw_path.replace("\\", "/")
        while posix_path.startswith("./"):
            posix_path = posix_path[2:]
        posix_path = posix_path.lstrip("/")
        lower_path = posix_path.lower()
        lower_base = posix_path.rsplit("/", 1)[-1].lower()

        is_test_path = lower_path == "tests" or lower_path.startswith("tests/")
        is_demo_vault = (
            lower_path == "examples/demo_vault"
            or lower_path.startswith("examples/demo_vault/")
        )

        if strict_non_corporate:
            if lower_path == "vault" or lower_path.startswith("vault/"):
                reasons.append(
                    f"Forbidden proprietary vault path staged ({dst_mode}): "
                    f"{posix_path} [{remote_desc}]"
                )
            if (
                lower_base == ".env" or lower_base.startswith(".env.")
            ) and lower_base != ".env.example":
                reasons.append(
                    f"Forbidden environment file staged: {posix_path} [{remote_desc}]"
                )
            if lower_base.endswith((".pbix", ".vpax")):
                reasons.append(
                    f"Forbidden Power BI binary/extract staged: {posix_path} [{remote_desc}]"
                )
            if lower_path == "exports" or lower_path.startswith("exports/"):
                reasons.append(
                    f"Forbidden exports artifact staged: {posix_path} [{remote_desc}]"
                )
            if not is_test_path and not is_demo_vault:
                if any(
                    tok in lower_base for tok in _DUMP_FILENAME_TOKENS
                ) or lower_base.endswith(".parquet"):
                    reasons.append(
                        f"Forbidden catalog/table dump file staged: "
                        f"{posix_path} [{remote_desc}]"
                    )

        if dst_mode == "160000":
            continue

        if is_test_path:
            continue

        try:
            blob_text = _read_staged_blob(repo_path, git_env, dst_sha)
        except RuntimeError as exc:
            reasons.append(f"Failed reading staged blob for {posix_path}: {exc}")
            continue

        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(blob_text):
                reasons.append(
                    f"Staged secret/credential detected ({label}) in: {posix_path}"
                )

        if strict_non_corporate and not is_demo_vault:
            if (
                _SQL_MULTIROW_INSERT_RE.search(blob_text)
                or len(_SQL_INSERT_STMT_RE.findall(blob_text)) >= 2
            ):
                reasons.append(
                    f"Forbidden bulk SQL table dump (INSERT INTO ... VALUES) staged in: "
                    f"{posix_path} [{remote_desc}]"
                )

    return (len(reasons) == 0), reasons


def _reconfigure_utf8_stderr() -> None:
    """Ensure UTF-8 output on Windows consoles without raising on captured streams."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for installing or executing the Project Poopdeck DLP hook."""
    _reconfigure_utf8_stderr()
    parser = argparse.ArgumentParser(
        prog="install_dlp_hook.py",
        description="Install or execute the Project Poopdeck pre-commit DLP guard.",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Target Git repository root directory (defaults to current directory).",
    )
    parser.add_argument(
        "--hook",
        action="store_true",
        help="Execute the pre-commit staged DLP check instead of installing the hook.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional path to Poopdeck YAML configuration file.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)

    if args.hook:
        try:
            cfg = (
                _resolve_dlp_config(repo_root, args.config)
                if args.config
                else None
            )
            ok, reasons = run_dlp_check(repo_root, config=cfg)
        except Exception as exc:
            print(
                f"{DLP_BANNER} Unexpected error executing DLP pre-commit check: {exc}",
                file=sys.stderr,
            )
            return 1

        if not ok:
            print(
                f"{DLP_BANNER} Commit rejected by Project Poopdeck DLP Guard:",
                file=sys.stderr,
            )
            for reason in reasons:
                print(f"  - {reason}", file=sys.stderr)
            return 1
        return 0

    try:
        hook_path = install_hook(repo_root)
    except Exception as exc:
        print(
            f"{DLP_BANNER} Failed to install pre-commit hook: {exc}",
            file=sys.stderr,
        )
        return 1

    print(f"Installed Project Poopdeck DLP pre-commit hook at: {hook_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
