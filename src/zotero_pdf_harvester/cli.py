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
import time
import urllib.parse
import urllib.request
from pathlib import Path

from pyzotero import zotero
from pyzotero._helpers import load_local_key

LOCAL_API = "http://127.0.0.1:23119/api/users/0"
SSL = ssl.create_default_context()


def normalize_doi(value: str | None) -> str:
    value = str(value or "").strip().lower().replace("\\_", "_")
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value if value.startswith("10.") and "/" in value else ""


def safe_name(doi: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "--", doi) + ".pdf"


class Harvester:
    def __init__(self, email: str, output: Path, timeout: int = 18, fallback_cli: str = ""):
        self.email, self.output, self.timeout = email, output, timeout
        self.fallback_cli = fallback_cli
        self.ua = f"zotero-pdf-harvester/0.1 (mailto:{email})"
        output.mkdir(parents=True, exist_ok=True)
        server_id, local_key = load_local_key()
        self.zotero = zotero.Zotero(0, "user", local=True, server_id=server_id, local_api_key=local_key)
        self.zotero.upload_timeout = 90

    def json(self, url: str, timeout: int = 10):
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "application/json"})
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
            return normalize_doi(hit.get("DOI")) if len(a & b) / max(1, len(a)) >= 0.78 else ""
        except Exception:
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

        def semantic():
            ident = urllib.parse.quote("DOI:" + doi, safe=":")
            p = (self.json(f"https://api.semanticscholar.org/graph/v1/paper/{ident}?fields=openAccessPdf")
                 .get("openAccessPdf") or {})
            return [(p.get("url"), "semantic_scholar")] if p.get("url") else []

        with cf.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(fn) for fn in (unpaywall, openalex, europepmc, crossref, semantic)]
            for future in futures:
                try:
                    found.extend(future.result(timeout=12))
                except Exception:
                    pass
        seen = set()
        return [(u, s) for u, s in found if u and u.startswith(("http://", "https://")) and not (u in seen or seen.add(u))]

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
            for url, candidate_source in self.resolvers(doi):
                if self.download(url, target):
                    source = candidate_source; break
        if not source and self.fallback(doi, target, fallback_timeout):
            source = "fetchpdf"
        if not source:
            return {"key": key, "doi": doi, "status": "not_found", "title": data.get("title", "")}
        return {"key": key, "doi": doi, "status": "ready", "source": source,
                "file": str(target), "title": data.get("title", "")}

    def attach(self, result):
        path = Path(result["file"])
        self.zotero.upload_attachments([{
            "itemType": "attachment", "linkMode": "imported_file", "title": "Full Text PDF",
            "filename": str(path.resolve()), "contentType": "application/pdf", "charset": "",
            "note": "", "tags": [], "relations": {},
        }], parentid=result["key"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", action="append", required=True, help="collection name or key; repeatable")
    parser.add_argument("--email", required=True, help="email for polite public API access")
    parser.add_argument("--output", default="downloads")
    parser.add_argument("--report", default="reports/latest.json")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout", type=int, default=18, help="HTTP timeout per PDF candidate")
    parser.add_argument("--fallback-cli", default="", help="optional fetchpdf executable")
    parser.add_argument("--fallback-timeout", type=int, default=40, help="hard OS timeout per fallback DOI")
    args = parser.parse_args()
    harvester = Harvester(args.email, Path(args.output), args.timeout, args.fallback_cli)
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
