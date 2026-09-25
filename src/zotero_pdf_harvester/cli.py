"""Concurrent legal-OA resolver with Zotero Local API attachment."""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from pyzotero import zotero
from pyzotero._helpers import load_local_key, save_local_key

LOCAL_API = "http://127.0.0.1:23119/api/users/0"
SSL = ssl.create_default_context()


def normalize_doi(value: str | None) -> str:
    value = str(value or "").strip().lower().replace("\\_", "_")
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value if value.startswith("10.") and "/" in value else ""


def safe_name(doi: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "--", doi) + ".pdf"


class Harvester:
    def __init__(self, email: str, output: Path, timeout: int = 18, fallback_cli: str = "",
                 core_key: str = "", browser_fallback: bool = False):
        self.email, self.output, self.timeout = email, output, timeout
        self.fallback_cli = shutil.which("fetchpdf") if fallback_cli == "auto" else fallback_cli
        self.core_key, self.browser_fallback = core_key, browser_fallback
        self.elsevier_key = os.environ.get("ELSEVIER_TDM_API_KEY", os.environ.get("ELSEVIER_API_KEY", ""))
        self.wiley_token = os.environ.get("WILEY_TDM_TOKEN", "")
        self.springer_key = os.environ.get("SPRINGER_API_KEY", "")
        self.browser_lock = threading.Lock()
        self.ua = f"zotero-pdf-harvester/0.2 (mailto:{email})"
        output.mkdir(parents=True, exist_ok=True)
        server_id, local_key = load_local_key()
        if not local_key:
            bootstrap = zotero.Zotero(0, "user", local=True)
            try:
                bootstrap.top(limit=1)
                print("首次运行：请在 Zotero 弹窗中选择“始终允许 / Always Allow”。", flush=True)
                result = bootstrap.authorize_local("Zotero PDF Harvester")
            except Exception as error:
                raise RuntimeError(
                    "无法连接或授权 Zotero。请启动 Zotero，并在 设置→高级 中允许本机应用通信。"
                ) from error
            if not result.get("remember"):
                raise RuntimeError("授权不是永久授权；请重新运行并在 Zotero 中选择“始终允许”。")
            server_id, local_key = bootstrap.server_id, result["key"]
            save_local_key(local_key, server_id)
        self.zotero = zotero.Zotero(0, "user", local=True, server_id=server_id, local_api_key=local_key)
        self.zotero.upload_timeout = 90

    def json(self, url: str, timeout: int = 10, headers: dict | None = None):
        request_headers = {"User-Agent": self.ua, "Accept": "application/json"}
        request_headers.update(headers or {})
        req = urllib.request.Request(url, headers=request_headers)
        with urllib.request.urlopen(req, timeout=timeout, context=SSL) as response:
            return json.load(response)

    def local(self, path: str):
        req = urllib.request.Request(LOCAL_API + path, headers={"User-Agent": self.ua})
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.load(response)

    def collections(self):
        return self.local("/collections?limit=100")

    def collection_key(self, name_or_key: str) -> str:
        for row in self.collections():
            if row["key"] == name_or_key or row.get("data", {}).get("name") == name_or_key:
                return row["key"]
        raise SystemExit(f"Zotero collection not found: {name_or_key}")

    def items(self, collection: str):
        out, start = [], 0
        while True:
            page = self.local(f"/collections/{collection}/items/top?limit=100&start={start}")
            out.extend(page)
            if len(page) < 100:
                return out
            start += len(page)

    def has_pdf(self, key: str) -> bool:
        return any(
            str(x.get("data", {}).get("contentType", "")).lower() == "application/pdf"
            or str(x.get("data", {}).get("filename", "")).lower().endswith(".pdf")
            for x in self.local(f"/items/{key}/children?limit=100")
        )

    def title_doi(self, title: str) -> str:
        if len(title) < 12:
            return ""
        query = urllib.parse.urlencode({"query.title": title, "rows": 1, "mailto": self.email})
        try:
            hit = self.json("https://api.crossref.org/works?" + query)["message"]["items"][0]
            actual = " ".join(hit.get("title") or [])
            a, b = set(re.findall(r"[a-z0-9]+", title.lower())), set(re.findall(r"[a-z0-9]+", actual.lower()))
            if len(a & b) / max(1, len(a)) >= 0.78:
                return normalize_doi(hit.get("DOI"))
        except Exception:
            pass
        # OpenAlex often retains DOI links for older repository records that
        # Crossref title search does not rank, but use a strict token threshold.
        try:
            params = urllib.parse.urlencode({"search": title, "per-page": 5, "mailto": self.email})
            for hit in self.json("https://api.openalex.org/works?" + params).get("results", []):
                actual = str(hit.get("title") or "")
                a = set(re.findall(r"[a-z0-9]+", title.lower()))
                b = set(re.findall(r"[a-z0-9]+", actual.lower()))
                if len(a & b) / max(1, len(a)) >= 0.88:
                    doi = normalize_doi(hit.get("doi"))
                    if doi:
                        return doi
        except Exception:
            pass
        return ""

    def resolvers(self, doi: str):
        quoted, found = urllib.parse.quote(doi), []

        def unpaywall():
            d = self.json(f"https://api.unpaywall.org/v2/{quoted}?email={self.email}")
            locs = ([d.get("best_oa_location")] if d.get("best_oa_location") else []) + (d.get("oa_locations") or [])
            return [(x.get("url_for_pdf") or x.get("url"), "unpaywall") for x in locs if x]

        def openalex():
            ident = urllib.parse.quote("https://doi.org/" + doi, safe="")
            d = self.json(f"https://api.openalex.org/works/{ident}?mailto={self.email}")
            locs = ([d.get("best_oa_location")] if d.get("best_oa_location") else []) + (d.get("locations") or [])
            return [(x.get("pdf_url"), "openalex") for x in locs if x and x.get("pdf_url")]

        def europepmc():
            q = urllib.parse.urlencode({"query": f'DOI:"{doi}"', "format": "json", "pageSize": 3})
            d = self.json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + q)
            out = []
            for x in d.get("resultList", {}).get("result", []):
                if x.get("pmcid"):
                    p = x["pmcid"]
                    out += [(f"https://europepmc.org/articles/{p}/bin/{p}.pdf", "europepmc"),
                            (f"https://pmc.ncbi.nlm.nih.gov/articles/{p}/pdf/", "pmc")]
            return out

        def crossref():
            d = self.json(f"https://api.crossref.org/works/{quoted}?mailto={self.email}")["message"]
            return [(x.get("URL"), "crossref") for x in d.get("link") or []
                    if "pdf" in str(x.get("content-type", "")).lower()]

        def openaire():
            # OpenAIRE aggregates institutional-repository records and exposes
            # a fulltext URL even when Unpaywall/OpenAlex have no OA location.
            q = urllib.parse.urlencode({"doi": doi, "format": "json", "size": 5})
            d = self.json("https://api.openaire.eu/search/publications?" + q)
            out = []
            for row in d.get("response", {}).get("results", {}).get("result", []):
                try:
                    result = row["metadata"]["oaf:entity"]["oaf:result"]
                    fulltext = result.get("fulltext")
                    values = fulltext if isinstance(fulltext, list) else [fulltext]
                    for value in values:
                        if isinstance(value, dict):
                            value = value.get("$") or value.get("value")
                        if isinstance(value, str):
                            out.append((value, "openaire"))
                except (KeyError, TypeError):
                    continue
            return out

        def springer():
            if not self.springer_key or not doi.startswith(("10.1007/", "10.1186/")):
                return []
            q = urllib.parse.urlencode({"q": f"doi:{doi}", "api_key": self.springer_key})
            d = self.json("https://api.springernature.com/openaccess/json?" + q)
            out = []
            for record in d.get("records", []):
                for link in record.get("url", []):
                    if isinstance(link, dict) and link.get("format") == "pdf" and link.get("value"):
                        out.append((link["value"], "springer_tdm"))
            return out

        def semantic():
            ident = urllib.parse.quote("DOI:" + doi, safe=":")
            p = (self.json(f"https://api.semanticscholar.org/graph/v1/paper/{ident}?fields=openAccessPdf")
                 .get("openAccessPdf") or {})
            return [(p.get("url"), "semantic_scholar")] if p.get("url") else []

        def core():
            if not self.core_key:
                return []
            q = urllib.parse.urlencode({"q": f"doi:{doi}", "limit": 5})
            d = self.json(
                f"https://api.core.ac.uk/v3/search/works?{q}", timeout=10,
                headers={"Authorization": f"Bearer {self.core_key}"},
            )
            out = []
            for x in d.get("results", []):
                u = x.get("downloadUrl") or x.get("fullTextLink")
                if u:
                    out.append((u, "core"))
            return out

        def ncbi_oa():
            q = urllib.parse.urlencode({"db": "pmc", "term": f"{doi}[doi]", "retmode": "json"})
            d = self.json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + q)
            ids = d.get("esearchresult", {}).get("idlist", [])
            return [(f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{x}/pdf", "ncbi_oa") for x in ids]

        with cf.ThreadPoolExecutor(max_workers=7) as pool:
            futures = [pool.submit(fn) for fn in (unpaywall, openalex, europepmc, crossref, openaire, springer, semantic, core, ncbi_oa)]
            for future in futures:
                try:
                    found.extend(future.result(timeout=12))
                except Exception:
                    pass
        seen = set()
        return [(u, s) for u, s in found if u and u.startswith(("http://", "https://")) and not (u in seen or seen.add(u))]

    def special_download(self, doi: str, target: Path) -> str:
        """Use publisher TDM endpoints only with the user's own credentials."""
        candidates = []
        if self.elsevier_key and doi.startswith("10.1016/"):
            candidates.append((
                f"https://api.elsevier.com/content/article/doi/{urllib.parse.quote(doi, safe='')}",
                {"X-ELS-APIKey": self.elsevier_key, "Accept": "application/pdf"}, "elsevier_tdm",
            ))
        if self.wiley_token and doi.startswith("10.1002/"):
            candidates.append((
                f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{urllib.parse.quote(doi, safe='')}",
                {"Wiley-TDM-Client-Token": self.wiley_token, "Accept": "application/pdf"}, "wiley_tdm",
            ))
        for url, headers, source in candidates:
            request = urllib.request.Request(url, headers={"User-Agent": self.ua, **headers})
            part = target.with_suffix(".part")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout, context=SSL) as response:
                    if source == "elsevier_tdm" and "not entitled" in response.headers.get("X-ELS-Status", "").lower():
                        continue
                    head = response.read(8192)
                    if not head.startswith(b"%PDF"):
                        continue
                    with part.open("wb") as out:
                        out.write(head); shutil.copyfileobj(response, out, 262144)
                if part.stat().st_size >= 8000:
                    os.replace(part, target)
                    return source
            except Exception:
                pass
            finally:
                part.unlink(missing_ok=True)
        return ""

    def download(self, url: str, target: Path) -> bool:
        request = urllib.request.Request(url, headers={
            "User-Agent": self.ua, "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.2",
            "Referer": "https://doi.org/",
        })
        part = target.with_suffix(".part")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=SSL) as response, part.open("wb") as out:
                head = response.read(8192)
                if not head.startswith(b"%PDF"):
                    return False
                out.write(head); shutil.copyfileobj(response, out, 262144)
            if part.stat().st_size < 8000:
                return False
            os.replace(part, target); return True
        except Exception:
            return False
        finally:
            part.unlink(missing_ok=True)

    def fallback(self, doi: str, target: Path, seconds: int) -> bool:
        if not self.fallback_cli:
            return False
        try:
            result = subprocess.run([self.fallback_cli, doi, str(target), "--email", self.email],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=seconds)
            return result.returncode == 0 and target.exists() and target.stat().st_size > 8000 and target.read_bytes()[:5].startswith(b"%PDF")
        except (OSError, subprocess.TimeoutExpired):
            return False

    def institutional_fallback(self, doi: str, target: Path) -> tuple[bool, str]:
        """Use the user's own institutional browser session; never bypasses access controls."""
        if not self.browser_fallback:
            return False, "disabled"
        try:
            from auto_paper_download.browser_fallback import browser_fallback_download
            from auto_paper_download.publishers import classify_publisher
        except ImportError:
            return False, "browser_helper_missing"
        with self.browser_lock:
            try:
                info = classify_publisher(doi)
                family = info.family if info else "unknown"
                result = browser_fallback_download(
                    doi=doi, output_dir=self.output / "_institutional",
                    publisher_family=family or "unknown", timeout_ms=max(15000, self.timeout * 1000),
                )
                saved = Path(result.saved_path) if result.success and result.saved_path else None
                if saved and saved.exists() and saved.read_bytes()[:5].startswith(b"%PDF"):
                    shutil.copy2(saved, target)
                    return True, "institutional_browser"
                return False, result.status or "browser_failed"
            except Exception:
                return False, "browser_error"

    def resolve(self, item, fallback_timeout: int):
        key, data = item["key"], item.get("data", {})
        if self.has_pdf(key):
            return {"key": key, "status": "existing", "title": data.get("title", "")}
        doi = normalize_doi(data.get("DOI")) or self.title_doi(str(data.get("title") or ""))
        if not doi:
            return {"key": key, "status": "no_identifier", "title": data.get("title", "")}
        target = self.output / safe_name(doi)
        source = "cache" if target.exists() else ""
        if not source:
            source = self.special_download(doi, target)
        if not source:
            for url, candidate_source in self.resolvers(doi):
                if self.download(url, target):
                    source = candidate_source; break
        if not source and self.fallback(doi, target, fallback_timeout):
            source = "fetchpdf"
        browser_status = ""
        if not source and self.browser_fallback:
            ok, browser_status = self.institutional_fallback(doi, target)
            if ok:
                source = "institutional_browser"
        if not source:
            result = {"key": key, "doi": doi, "status": "not_found", "title": data.get("title", "")}
            if browser_status:
                result["browser_status"] = browser_status
            return result
        return {"key": key, "doi": doi, "status": "ready", "source": source,
                "file": str(target), "title": data.get("title", "")}

    def attach(self, result):
        path = Path(result["file"])
        self.zotero.upload_attachments([{
            "itemType": "attachment", "linkMode": "imported_file", "title": "Full Text PDF",
            "filename": str(path.resolve()), "contentType": "application/pdf", "charset": "",
            "note": "", "tags": [], "relations": {},
        }], parentid=result["key"])


def env_file(path: Path = Path(".env")):
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1); out[k.strip()] = v.strip().strip('\"')
    return out

def main():
    env = env_file()
    # Child tools (fetchpdf, publisher clients, Playwright fallback) read their
    # credentials from the process environment. Keep explicit shell variables
    # authoritative while making values from this project's .env available.
    for key, value in env.items():
        os.environ.setdefault(key, value)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", action="append", required=True, help="collection name or key; repeatable")
    parser.add_argument("--email", default=env.get("ZPH_EMAIL", os.environ.get("ZPH_EMAIL", "")), help="email for polite public API access")
    parser.add_argument("--output", default="downloads")
    parser.add_argument("--report", default="reports/latest.json")
    parser.add_argument("--workers", type=int, default=int(env.get("ZPH_WORKERS", 16)))
    parser.add_argument("--timeout", type=int, default=18, help="HTTP timeout per PDF candidate")
    parser.add_argument("--fallback-cli", default=env.get("ZPH_FALLBACK_CLI", "auto"), help="fetchpdf executable; 'auto' detects it")
    parser.add_argument("--fallback-timeout", type=int, default=40, help="hard OS timeout per fallback DOI")
    parser.add_argument(
        "--core-key",
        default=env.get("CORE_API_KEY", env.get("COREAPIKEY", os.environ.get("CORE_API_KEY", os.environ.get("COREAPIKEY", "")))),
        help="optional CORE API key",
    )
    parser.add_argument("--institutional-browser", action="store_true", help="use a persistent browser profile and your institution login")
    args = parser.parse_args()
    if not args.email:
        parser.error("--email is required, or set ZPH_EMAIL in .env")
    harvester = Harvester(args.email, Path(args.output), args.timeout, args.fallback_cli,
                          args.core_key, args.institutional_browser)
    keys = [harvester.collection_key(x) for x in args.collection]
    raw = [item for key in keys for item in harvester.items(key)]
    seen, items = set(), []
    for item in raw:
        if item["key"] not in seen:
            seen.add(item["key"]); items.append(item)
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(harvester.resolve, item, args.fallback_timeout) for item in items]
        for future in cf.as_completed(futures):
            try:
                result = future.result()
                if result["status"] == "ready":
                    harvester.attach(result); result["status"] = "downloaded"
                    print(f"attached {result['doi']} [{result['source']}]", flush=True)
            except Exception as error:
                result = {"status": "failed", "error": str(error)}
            results.append(result)
            if len(results) % 10 == 0:
                print(f"progress {len(results)}/{len(items)}", flush=True)
    counts = {status: sum(x["status"] == status for x in results) for status in sorted({x["status"] for x in results})}
    report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "collections": keys, "counts": counts, "items": results}
    report_path = Path(args.report); report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
