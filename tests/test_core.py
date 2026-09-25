import os
import xml.etree.ElementTree as ET

from zotero_pdf_harvester.cli import Harvester, apply_env, env_file, normalize_doi, safe_name


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


def test_identifier_pmid():
    assert Harvester.identifier_pmid({"extra": "PMID: 26474635"}) == "26474635"
    assert Harvester.identifier_pmid({"PMID": "19221574"}) == "19221574"
    assert Harvester.identifier_pmid({"extra": "PMCID: PMC123"}) == ""


def test_html_pdf_candidates_extracts_standard_metadata():
    harvester = object.__new__(Harvester)
    body = b'<html><meta name="citation_pdf_url" content="/article/file.pdf"></html>'
    candidates, doi = harvester.html_pdf_candidates(body, "https://journal.example/paper")
    assert candidates == [("https://journal.example/article/file.pdf", "publisher_meta")]
    assert doi == ""
