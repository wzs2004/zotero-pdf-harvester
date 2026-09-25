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
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from bs4 import BeautifulSoup
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
        self.ncbi_lock = threading.Lock()
        self.ncbi_last_request = 0.0
        self.ncbi_key = os.environ.get("NCBI_API_KEY", "")
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

    def ncbi_xml(self, endpoint: str, params: dict) -> ET.Element:
        """Call NCBI E-utilities within its rate limit and retry transients."""
        params = dict(params)
        params.update({"tool": "zotero-pdf-harvester", "email": self.email})
        if self.ncbi_key:
            params["api_key"] = self.ncbi_key
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/" + endpoint + "?" + urllib.parse.urlencode(params)
        interval = 0.11 if self.ncbi_key else 0.34
        last_error = None
        for attempt in range(3):
            try:
                with self.ncbi_lock:
                    delay = interval - (time.monotonic() - self.ncbi_last_request)
                    if delay > 0:
                        time.sleep(delay)
                    req = urllib.request.Request(url, headers={"User-Agent": self.ua})
                    try:
                        with urllib.request.urlopen(req, timeout=15, context=SSL) as response:
                            body = response.read()
                    finally:
                        self.ncbi_last_request = time.monotonic()
                return ET.fromstring(body)
            except Exception as error:
                last_error = error
                time.sleep(0.5 * (attempt + 1))
        raise last_error or RuntimeError("NCBI request failed")

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
        try:
            query = urllib.parse.urlencode({"query": f'TITLE:"{title}"', "format": "json", "pageSize": 5})
            hits = self.json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + query)
            for hit in hits.get("resultList", {}).get("result", []):
                actual = str(hit.get("title") or "")
                a = set(re.findall(r"[a-z0-9]+", title.lower()))
                b = set(re.findall(r"[a-z0-9]+", actual.lower()))
                if len(a & b) / max(1, len(a)) >= 0.85:
                    doi = normalize_doi(hit.get("doi"))
                    if doi:
                        return doi
        except Exception:
            pass
        return ""

    @staticmethod
    def identifier_pmid(data: dict) -> str:
        """Return a PMID stored in Zotero's Extra field or a dedicated field."""
        for value in (data.get("PMID"), data.get("pmid")):
            if re.fullmatch(r"\d{6,9}", str(value or "").strip()):
                return str(value).strip()
        extra = str(data.get("extra") or "")
        match = re.search(r"(?im)^\s*PMID\s*:\s*(\d{6,9})\s*$", extra)
        return match.group(1) if match else ""

    def title_identifiers(self, title: str) -> tuple[str, str]:
        """Strict Europe PMC title match, returning DOI and PMID when present."""
        if len(title) < 12:
            return "", ""
        try:
            query = urllib.parse.urlencode({"query": f'TITLE:"{title}"', "format": "json", "pageSize": 8})
            hits = self.json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + query)
            wanted = set(re.findall(r"[a-z0-9]+", title.lower()))
            for hit in hits.get("resultList", {}).get("result", []):
                actual = set(re.findall(r"[a-z0-9]+", str(hit.get("title") or "").lower()))
                if len(wanted & actual) / max(1, len(wanted)) >= 0.88:
                    return normalize_doi(hit.get("doi")), str(hit.get("pmid") or "")
        except Exception:
            pass
        return "", ""

    def identifier_doi(self, data: dict) -> str:
        doi = normalize_doi(data.get("DOI"))
        if doi:
            return doi
        extra = str(data.get("extra") or "")
        pmid = self.identifier_pmid(data)
        if pmid:
            try:
                root = self.ncbi_xml("efetch.fcgi", {
                    "db": "pubmed", "id": pmid, "retmode": "xml",
                })
                for node in root.findall(".//ArticleId"):
                    if node.attrib.get("IdType") == "doi":
                        doi = normalize_doi(node.text)
                        if doi:
                            return doi
            except Exception:
                pass
        return self.title_doi(str(data.get("title") or ""))

    def page_pdf_candidates(self, url: str) -> tuple[list[tuple[str, str]], str]:
        """Extract publisher-declared PDF URLs and DOI metadata from a free page."""
        req = urllib.request.Request(url, headers={
            "User-Agent": self.ua,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.5",
        })
        with urllib.request.urlopen(req, timeout=self.timeout, context=SSL) as response:
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "").lower()
            body = response.read(4 * 1024 * 1024)
        if body.startswith(b"%PDF"):
            return [(final_url, "pubmed_linkout")], ""
        if "html" not in content_type and b"<html" not in body[:4096].lower():
            return [], ""
        return self.html_pdf_candidates(body, final_url)

    def html_pdf_candidates(self, body: bytes, final_url: str) -> tuple[list[tuple[str, str]], str]:
        """Parse standard scholarly HTML metadata without site-specific scraping."""
        soup = BeautifulSoup(body, "html.parser")
        found, doi = [], ""
        pdf_meta_names = {
            "citation_pdf_url", "dc.identifier.pdf", "eprints.document_url",
            "wkhealth_pdf_url", "pdf_url",
        }
        for meta in soup.find_all("meta"):
            name = str(meta.get("name") or meta.get("property") or "").strip().lower()
            value = str(meta.get("content") or "").strip()
            if not value:
                continue
            if name in pdf_meta_names or ("pdf" in name and value.startswith(("http://", "https://", "/"))):
                found.append((urllib.parse.urljoin(final_url, value), "publisher_meta"))
            if name in {"citation_doi", "dc.identifier", "dc.identifier.doi", "prism.doi"}:
                doi = doi or normalize_doi(value)
        for link in soup.find_all("link"):
            href = str(link.get("href") or "").strip()
            kind = str(link.get("type") or "").lower()
            if href and (kind == "application/pdf" or "pdf" in str(link.get("title") or "").lower()):
                found.append((urllib.parse.urljoin(final_url, href), "publisher_link"))
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            label = " ".join(anchor.stripped_strings).lower()
            if href and (href.lower().split("?", 1)[0].endswith(".pdf") or label in {"pdf", "全文pdf", "pdf全文"}):
                found.append((urllib.parse.urljoin(final_url, href), "publisher_link"))
        seen = set()
        return [(u, s) for u, s in found if not (u in seen or seen.add(u))], doi

    def title_resolvers(self, title: str) -> list[tuple[str, str]]:
        """Find repository copies whose titles strictly match the Zotero record."""
        if len(title) < 12:
            return []
        wanted = set(re.findall(r"[a-z0-9]+", title.lower()))
        found = []

        def matches(actual: str, threshold: float = 0.9) -> bool:
            words = set(re.findall(r"[a-z0-9]+", str(actual or "").lower()))
            return len(wanted & words) / max(1, len(wanted)) >= threshold

        def openalex_title():
            params = urllib.parse.urlencode({"search": title, "per-page": 5, "mailto": self.email})
            data = self.json("https://api.openalex.org/works?" + params)
            out = []
            for row in data.get("results", []):
                if not matches(row.get("title", "")):
                    continue
                locations = ([row.get("best_oa_location")] if row.get("best_oa_location") else []) + (row.get("locations") or [])
                for location in locations:
                    if not location:
                        continue
                    for url in (location.get("pdf_url"), location.get("landing_page_url")):
                        if url:
                            out.append((url, "openalex_title"))
            return out

        def semantic_title():
            params = urllib.parse.urlencode({"query": title, "limit": 5, "fields": "title,openAccessPdf"})
            data = self.json("https://api.semanticscholar.org/graph/v1/paper/search?" + params)
            out = []
            for row in data.get("data", []):
                pdf = row.get("openAccessPdf") or {}
                if matches(row.get("title", "")) and pdf.get("url"):
                    out.append((pdf["url"], "semantic_title"))
            return out

        def hal_title():
            params = urllib.parse.urlencode({
                "q": f'title_t:\"{title}\"', "fl": "title_s,fileMain_s,files_s", "rows": 5, "wt": "json",
            })
            data = self.json("https://api.archives-ouvertes.fr/search/?" + params)
            out = []
            for row in data.get("response", {}).get("docs", []):
                actual = row.get("title_s") or ""
                if isinstance(actual, list):
                    actual = " ".join(actual)
                if not matches(actual):
                    continue
                for url in [row.get("fileMain_s")] + list(row.get("files_s") or []):
                    if isinstance(url, str):
                        out.append((url, "hal_title"))
            return out

        with cf.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(fn) for fn in (openalex_title, semantic_title, hal_title)]
            for future in futures:
                try:
                    found.extend(future.result(timeout=12))
                except Exception:
                    pass
        seen = set()
        return [(u, s) for u, s in found if u.startswith(("http://", "https://")) and not (u in seen or seen.add(u))]

    def pmid_resolvers(self, pmid: str) -> tuple[list[tuple[str, str]], str]:
        """Resolve legal free-full-text links advertised by PubMed and Europe PMC."""
        found, discovered_doi = [], ""
        try:
            query = urllib.parse.urlencode({"query": f"EXT_ID:{pmid} AND SRC:MED", "format": "json", "pageSize": 3})
            data = self.json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + query)
            for row in data.get("resultList", {}).get("result", []):
                discovered_doi = discovered_doi or normalize_doi(row.get("doi"))
                if row.get("pmcid"):
                    pmcid = row["pmcid"]
                    found.extend([
                        (f"https://europepmc.org/articles/{pmcid}/bin/{pmcid}.pdf", "europepmc"),
                        (f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/", "pmc"),
                    ])
        except Exception:
            pass
        try:
            root = self.ncbi_xml("elink.fcgi", {
                "dbfrom": "pubmed", "id": pmid, "cmd": "llinks", "retmode": "xml",
            })
            pages = []
            for obj in root.findall(".//ObjUrl"):
                attrs = {str(node.text or "").strip().lower() for node in obj.findall("Attribute")}
                category = str(obj.findtext("Category", "")).strip().lower()
                url = obj.findtext("Url", "").strip()
                # PubMed LinkOut also returns patient-information and assay
                # portals as "free resources". Only scholarly full-text links
                # may be considered PDF candidates for this citation.
                if url and category == "full text sources" and ("free resource" in attrs or "free" in attrs):
                    pages.append(url)
            for page in pages:
                try:
                    candidates, page_doi = self.page_pdf_candidates(page)
                    found.extend(candidates)
                    discovered_doi = discovered_doi or page_doi
                except Exception:
                    continue
        except Exception:
            pass
        seen = set()
        return [(u, s) for u, s in found if not (u in seen or seen.add(u))], discovered_doi

    def doi_pmid(self, doi: str) -> str:
        """Map a DOI to PMID so PubMed's curated free LinkOut can be tried."""
        try:
            query = urllib.parse.urlencode({"query": f'DOI:"{doi}"', "format": "json", "pageSize": 3})
            data = self.json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + query)
            for row in data.get("resultList", {}).get("result", []):
                if normalize_doi(row.get("doi")) == doi and row.get("pmid"):
                    return str(row["pmid"])
        except Exception:
            pass
        return ""

    def resolvers(self, doi: str):
        quoted, found = urllib.parse.quote(doi), []

        def unpaywall():
            d = self.json(f"https://api.unpaywall.org/v2/{quoted}?email={self.email}")
            locs = ([d.get("best_oa_location")] if d.get("best_oa_location") else []) + (d.get("oa_locations") or [])
            return [(url, "unpaywall") for x in locs if x for url in (x.get("url_for_pdf"), x.get("url")) if url]

        def openalex():
            ident = urllib.parse.quote("https://doi.org/" + doi, safe="")
            d = self.json(f"https://api.openalex.org/works/{ident}?mailto={self.email}")
            locs = ([d.get("best_oa_location")] if d.get("best_oa_location") else []) + (d.get("locations") or [])
            return [(url, "openalex") for x in locs if x for url in (x.get("pdf_url"), x.get("landing_page_url")) if url]

        def datacite():
            d = self.json(f"https://api.datacite.org/dois/{quoted}")
            attrs = d.get("data", {}).get("attributes", {})
            out = []
            if attrs.get("url"):
                out.append((attrs["url"], "datacite"))
            for value in attrs.get("contentUrl") or []:
                if isinstance(value, str):
                    out.append((value, "datacite"))
            return out

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

        def doaj():
            # DOAJ has direct full-text links for journals that are fully OA,
            # including records occasionally missed by Unpaywall.
            query = urllib.parse.quote(f'bibjson.identifier.id:"{doi}"', safe="")
            d = self.json(f"https://doaj.org/api/search/articles/{query}?pageSize=5")
            out = []
            for row in d.get("results", []):
                for link in row.get("bibjson", {}).get("link", []) or []:
                    url = link.get("url") if isinstance(link, dict) else ""
                    kind = str(link.get("type", "")).lower() if isinstance(link, dict) else ""
                    if url and ("fulltext" in kind or "pdf" in kind or url.lower().endswith(".pdf")):
                        out.append((url, "doaj"))
            return out

        def zenodo():
            q = urllib.parse.urlencode({"q": f'doi:"{doi}"', "size": 10})
            d = self.json("https://zenodo.org/api/records?" + q)
            out = []
            for record in d.get("hits", {}).get("hits", []):
                for file in record.get("files", []) or []:
                    links = file.get("links", {}) if isinstance(file, dict) else {}
                    url = links.get("content") or links.get("self")
                    name = str(file.get("key") or "").lower() if isinstance(file, dict) else ""
                    mime = str(file.get("mimetype") or "").lower() if isinstance(file, dict) else ""
                    if url and (name.endswith(".pdf") or mime == "application/pdf"):
                        out.append((url, "zenodo"))
            return out

        def hal():
            q = urllib.parse.urlencode({
                "q": f'doiId_s:"{doi}"',
                "fl": "fileMain_s,files_s,uri_s",
                "rows": 10,
                "wt": "json",
            })
            d = self.json("https://api.archives-ouvertes.fr/search/?" + q)
            out = []
            for row in d.get("response", {}).get("docs", []):
                values = [row.get("fileMain_s")] + list(row.get("files_s") or [])
                for url in values:
                    if isinstance(url, str) and url.startswith(("http://", "https://")):
                        out.append((url, "hal"))
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

        with cf.ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(fn) for fn in (
                unpaywall, openalex, europepmc, crossref, openaire, doaj,
                zenodo, hal, datacite, springer, semantic, core, ncbi_oa,
            )]
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

    def download(self, url: str, target: Path, inspect_html: bool = True) -> bool:
        request = urllib.request.Request(url, headers={
            "User-Agent": self.ua, "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.2",
            "Referer": "https://doi.org/",
        })
        part = target.with_suffix(".part")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=SSL) as response:
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "").lower()
                head = response.read(8192)
                if not head.startswith(b"%PDF"):
                    if inspect_html and ("html" in content_type or b"<html" in head.lower()):
                        body = head + response.read(4 * 1024 * 1024 - len(head))
                        candidates, _ = self.html_pdf_candidates(body, final_url)
                        for candidate, _source in candidates:
                            if candidate != final_url and self.download(candidate, target, inspect_html=False):
                                return True
                    return False
                with part.open("wb") as out:
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
                if result.status == "auth_redirect":
                    self.institutional_login(doi)
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

    def institutional_login(self, doi: str):
        """Keep a persistent browser open while the user completes school SSO."""
        try:
            from playwright.sync_api import sync_playwright
            profile = Path(os.environ.get(
                "BROWSER_FALLBACK_PROFILE",
                Path.home() / ".cache" / "auto_paper_download" / "browser_profile",
            )).expanduser()
            profile.mkdir(parents=True, exist_ok=True)
            channel = os.environ.get("BROWSER_FALLBACK_CHANNEL") or ("chrome" if sys.platform == "darwin" else None)
            wait_seconds = int(os.environ.get("BROWSER_LOGIN_WAIT_SECONDS", "300"))
            print(f"需要学校登录：浏览器将保持最多 {wait_seconds} 秒；完成登录后会自动继续。", flush=True)
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    str(profile), channel=channel, headless=False, accept_downloads=True,
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(f"https://doi.org/{doi}", wait_until="domcontentloaded", timeout=60000)
                deadline = time.time() + wait_seconds
                while time.time() < deadline:
                    current = page.url.lower()
                    if not any(x in current for x in ("login", "signin", "sso", "shibboleth", "openathens", "oauth", "saml")):
                        page.wait_for_timeout(5000)
                        break
                    page.wait_for_timeout(2000)
                context.close()
        except Exception as error:
            print(f"学校登录窗口未完成：{type(error).__name__}", flush=True)

    def institutional_batch(self, pending: list[dict]) -> list[dict]:
        """Retry missing DOI records in one persistent, authenticated browser."""
        if not pending:
            return pending
        try:
            from playwright.sync_api import sync_playwright
            from auto_paper_download.browser_fallback import GENERIC_PDF_SELECTORS, PUBLISHER_PDF_SELECTORS
            from auto_paper_download.publishers import classify_publisher
        except ImportError as error:
            print(f"机构浏览器不可用：{error}", flush=True)
            return pending
        profile = Path(os.environ.get("BROWSER_FALLBACK_PROFILE", "browser-profile")).expanduser()
        profile.mkdir(parents=True, exist_ok=True)
        channel = os.environ.get("BROWSER_FALLBACK_CHANNEL") or ("chrome" if sys.platform == "darwin" else None)
        login_wait = int(os.environ.get("BROWSER_LOGIN_WAIT_SECONDS", "300"))
        auth_words = ("login", "signin", "sso", "shibboleth", "openathens", "oauth", "saml", "ezproxy")
        print(f"机构浏览器批处理：共 {len(pending)} 条，复用同一登录会话。", flush=True)
        try:
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    str(profile), channel=channel, headless=False, accept_downloads=True,
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(max(15000, self.timeout * 1000))
                for index, result in enumerate(pending, 1):
                    doi = result.get("doi") or ""
                    target = self.output / safe_name(doi)
                    try:
                        page.goto(f"https://doi.org/{doi}", wait_until="domcontentloaded", timeout=60000)
                        if any(word in page.url.lower() for word in auth_words):
                            print(f"需要学校登录；窗口将等待最多 {login_wait} 秒。", flush=True)
                            deadline = time.time() + login_wait
                            while time.time() < deadline and any(word in page.url.lower() for word in auth_words):
                                page.wait_for_timeout(2000)
                        info = classify_publisher(doi)
                        family = info.family if info else "unknown"
                        selectors = list(PUBLISHER_PDF_SELECTORS.get(family, ())) + list(GENERIC_PDF_SELECTORS)
                        locator = None
                        for selector in selectors:
                            candidate = page.locator(selector).first
                            if candidate.count() and candidate.is_visible():
                                locator = candidate
                                break
                        if locator is not None:
                            href = locator.get_attribute("href")
                            if href:
                                url = urllib.parse.urljoin(page.url, href)
                                response = context.request.get(
                                    url, headers={"Accept": "application/pdf,*/*;q=0.8", "Referer": page.url},
                                    timeout=max(30000, self.timeout * 1000),
                                )
                                body = response.body()
                                if response.ok and body.startswith(b"%PDF") and len(body) >= 8000:
                                    target.write_bytes(body)
                        if target.exists() and target.read_bytes()[:5] == b"%PDF":
                            result.update(status="ready", source="institutional_browser", file=str(target))
                            self.attach(result)
                            result["status"] = "downloaded"
                            print(f"attached {doi} [institutional_browser]", flush=True)
                        else:
                            result["browser_status"] = "not_entitled_or_no_pdf_link"
                    except Exception as error:
                        result["browser_status"] = type(error).__name__
                    if index % 10 == 0:
                        print(f"institutional progress {index}/{len(pending)}", flush=True)
                context.close()
        except Exception as error:
            print(f"机构浏览器批处理失败：{type(error).__name__}: {error}", flush=True)
        return pending

    def resolve(self, item, fallback_timeout: int):
        key, data = item["key"], item.get("data", {})
        if self.has_pdf(key):
            return {"key": key, "status": "existing", "title": data.get("title", "")}
        pmid = self.identifier_pmid(data)
        doi = self.identifier_doi(data)
        if not doi and not pmid:
            title_doi, title_pmid = self.title_identifiers(str(data.get("title") or ""))
            doi, pmid = title_doi, title_pmid
        if not doi and not pmid:
            return {"key": key, "status": "no_identifier", "title": data.get("title", "")}
        identifier = doi or f"pmid-{pmid}"
        target = self.output / safe_name(identifier)
        source = "cache" if target.exists() else ""
        if not source and data.get("url"):
            if self.download(str(data["url"]), target):
                source = "zotero_url"
        if not source and doi:
            source = self.special_download(doi, target)
        if not source and doi:
            for url, candidate_source in self.resolvers(doi):
                if self.download(url, target):
                    source = candidate_source; break
        if not source and doi:
            pmid = pmid or self.doi_pmid(doi)
            try:
                candidates, _ = self.page_pdf_candidates("https://doi.org/" + doi)
                for url, candidate_source in candidates:
                    if self.download(url, target):
                        source = candidate_source; break
            except Exception:
                pass
        if not source and pmid:
            candidates, discovered_doi = self.pmid_resolvers(pmid)
            doi = doi or discovered_doi
            for url, candidate_source in candidates:
                if self.download(url, target):
                    source = candidate_source; break
        if not source:
            for url, candidate_source in self.title_resolvers(str(data.get("title") or "")):
                if self.download(url, target):
                    source = candidate_source; break
        if not source and doi and self.fallback(doi, target, fallback_timeout):
            source = "fetchpdf"
        if not source:
            result = {"key": key, "doi": doi, "pmid": pmid, "status": "not_found", "title": data.get("title", "")}
            return result
        return {"key": key, "doi": doi, "pmid": pmid, "status": "ready", "source": source,
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


def apply_env(env: dict, base: Path = Path.cwd()):
    """Expose .env and resolve project-relative browser profile paths."""
    for key, value in env.items():
        if key == "BROWSER_FALLBACK_PROFILE" and value and not Path(value).expanduser().is_absolute():
            value = str((base / value).resolve())
        os.environ.setdefault(key, value)

def main():
    env = env_file()
    # Child tools (fetchpdf, publisher clients, Playwright fallback) read their
    # credentials from the process environment. Keep explicit shell variables
    # authoritative while making values from this project's .env available.
    apply_env(env)
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
    if args.institutional_browser:
        pending = [result for result in results if result.get("status") == "not_found" and result.get("doi")]
        harvester.institutional_batch(pending)
    counts = {status: sum(x["status"] == status for x in results) for status in sorted({x["status"] for x in results})}
    report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "collections": keys, "counts": counts, "items": results}
    report_path = Path(args.report); report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
