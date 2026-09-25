import os

from zotero_pdf_harvester.cli import apply_env, env_file, normalize_doi, safe_name


def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1000/ABC\\_1") == "10.1000/abc_1"
    assert normalize_doi("") == ""


def test_safe_name():
    assert safe_name("10.1000/a/b") == "10.1000--a--b.pdf"


def test_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# ignored\nZPH_EMAIL="user@example.com"\nCORE_API_KEY=abc\n')
    assert env_file(path) == {"ZPH_EMAIL": "user@example.com", "CORE_API_KEY": "abc"}


def test_apply_env_resolves_browser_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("BROWSER_FALLBACK_PROFILE", raising=False)
    apply_env({"BROWSER_FALLBACK_PROFILE": "browser-profile"}, tmp_path)
    assert os.environ["BROWSER_FALLBACK_PROFILE"] == str(tmp_path / "browser-profile")
