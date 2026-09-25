from zotero_pdf_harvester.cli import env_file, normalize_doi, safe_name


def test_normalize_doi():
    assert normalize_doi("https://doi.org/10.1000/ABC\\_1") == "10.1000/abc_1"
    assert normalize_doi("") == ""


def test_safe_name():
    assert safe_name("10.1000/a/b") == "10.1000--a--b.pdf"


def test_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# ignored\nZPH_EMAIL="user@example.com"\nCORE_API_KEY=abc\n')
    assert env_file(path) == {"ZPH_EMAIL": "user@example.com", "CORE_API_KEY": "abc"}
