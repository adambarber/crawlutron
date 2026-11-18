"""Lazy package root: no C extension on base import, loud hints for regex-backed
names, an observable PSL fallback. Probes run in subprocesses with an import
blocker, so no installation state is assumed."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from textwrap import dedent

from tests._walk_primitives import REPO_ROOT

_SAFE_URL_SRC = REPO_ROOT / "packages" / "safe-url" / "src"


def _run_probe(tmp_path: pathlib.Path, code: str, block: str | None = None) -> subprocess.CompletedProcess:
    """Run ``code`` in a subprocess with only the safe-url src tree on PYTHONPATH;
    when ``block`` is given, a meta-path finder raises ImportError for that
    top-level package and its submodules."""
    lines = ["import sys"]
    if block is not None:
        lines.extend(
            [
                "import importlib.abc",
                "",
                "class _Blocker(importlib.abc.MetaPathFinder):",
                "    def __init__(self, name: str) -> None:",
                "        self.name = name",
                "",
                "    def find_spec(self, fullname, path, target=None):",
                '        if fullname == self.name or fullname.startswith(self.name + "."):',
                '            raise ImportError(f"blocked: {fullname}")',
                "        return None",
                "",
                "sys.meta_path.insert(0, _Blocker(" + repr(block) + "))",
                "",
            ]
        )
    lines.append(dedent(code).strip())
    probe = tmp_path / "probe.py"
    probe.write_text("\n".join(lines) + "\n")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SAFE_URL_SRC)
    return subprocess.run([sys.executable, str(probe)], env=env, capture_output=True, text=True)


def test_base_import_pulls_no_c_extension(tmp_path):
    """With ``regex`` blocked, importing the root and the normalized_url
    submodule succeeds and admits a URL -- the C extension is never reached."""
    proc = _run_probe(
        tmp_path,
        """
        import safe_url
        import safe_url.normalized_url as nu
        from safe_url import HostPolicy, NormalizedUrl, admit_or_none

        url = NormalizedUrl("https://Example.COM/a/?utm_source=x#frag", policy=HostPolicy.unrestricted())
        assert url.raw == "https://example.com/a/"
        assert url.site == "example.com"
        assert admit_or_none("http://example.com/x", policy=HostPolicy.unrestricted()) is not None
        """,
        block="regex",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_regex_backed_name_raises_named_error_with_hint(tmp_path):
    """Touching a regex-backed name without the [regex] extra raises
    RegexUnavailable carrying the install hint, never a bare ImportError."""
    proc = _run_probe(
        tmp_path,
        """
        import safe_url

        try:
            safe_url.scope_allows
        except safe_url.RegexUnavailable as error:
            assert "crawlutron-safe-url[regex]" in str(error)
            raise SystemExit(0)
        raise SystemExit("scope_allows did not raise RegexUnavailable")
        """,
        block="regex",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_from_import_regex_backed_name_raises_named_error(tmp_path):
    """``from safe_url import scope_allows`` raises the same named error
    without the extra, so a top-level import cannot silently degrade."""
    proc = _run_probe(
        tmp_path,
        """
        import safe_url

        try:
            from safe_url import scope_allows
        except safe_url.RegexUnavailable as error:
            assert "crawlutron-safe-url[regex]" in str(error)
            raise SystemExit(0)
        raise SystemExit("from-import did not raise RegexUnavailable")
        """,
        block="regex",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_regex_backed_names_resolve_when_extra_installed():
    """With the extra present the public surface is byte-compatible: the same
    names import and behave exactly as before."""
    import safe_url

    compiled = safe_url.compile_untrusted(r"example\.com/keep/")
    assert safe_url.scope_allows(compiled, "https://example.com/keep/page?x=1") is True
    assert safe_url.scope_allows(compiled, "https://example.com/drop/page") is False


def test_psl_absent_answers_heuristic_explicitly(tmp_path):
    """Without publicsuffixlist the fallback answers and reports itself:
    psl_available() is False and a multi-label suffix keys the heuristic."""
    proc = _run_probe(
        tmp_path,
        """
        import safe_url

        assert safe_url.psl_available() is False
        assert safe_url.registrable_domain("a.github.io") == "github.io"
        assert safe_url.is_public_suffix("github.io") is False
        """,
        block="publicsuffixlist",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_psl_present_answers_from_dataset(tmp_path):
    """With the dataset present the same hosts key the real registrable domain
    and psl_available() reports True -- the fallback is never silent."""
    proc = _run_probe(
        tmp_path,
        """
        import safe_url

        assert safe_url.psl_available() is True
        assert safe_url.registrable_domain("a.github.io") == "a.github.io"
        assert safe_url.is_public_suffix("github.io") is True
        """,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
