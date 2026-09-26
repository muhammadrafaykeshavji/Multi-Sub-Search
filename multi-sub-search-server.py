#!/usr/bin/env python3
"""Serve multi-sub-search.html and proxy search (Reddit blocks bare scrapers)."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML = ROOT / "multi-sub-search.html"
PORT = 8765
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _get(url: str, timeout: int = 25, accept: str = "application/json,text/plain,*/*") -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def _post(url: str, form: dict, timeout: int = 25) -> bytes:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://duckduckgo.com/",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def _as_reddit_listing(body: bytes) -> bytes:
    """Raise if Reddit returned a challenge/login HTML page with HTTP 200."""
    text = body.lstrip()
    if text.startswith(b"<") or b"Welcome to Reddit" in body[:800]:
        raise RuntimeError("Reddit returned HTML instead of JSON")
    data = json.loads(body)
    if not (isinstance(data, dict) and isinstance(data.get("data"), dict)):
        raise RuntimeError("Unexpected Reddit JSON shape")
    if "children" not in data["data"]:
        raise RuntimeError("Reddit JSON missing children")
    return body


def _listing_from_rows(rows) -> bytes:
    if not isinstance(rows, list):
        rows = []
    children = []
    for d in rows:
        if not isinstance(d, dict):
            continue
        permalink = d.get("permalink") or (
            f"/comments/{d['id']}/" if d.get("id") else "#"
        )
        children.append(
            {
                "kind": "t3",
                "data": {
                    "title": d.get("title") or "",
                    "author": d.get("author") or "[deleted]",
                    "permalink": permalink,
                    "created_utc": d.get("created_utc") or 0,
                    "selftext": d.get("selftext") or "",
                    "num_comments": d.get("num_comments") or 0,
                    "score": d.get("score") or 0,
                },
            }
        )
    return json.dumps({"kind": "Listing", "data": {"children": children}}).encode()


def _parse_ddg_html(html: str, sub: str, limit: int) -> bytes:
    blocks = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        flags=re.I | re.S,
    )
    # lite layout sometimes uses different anchors
    if not blocks:
        blocks = re.findall(
            r'href="([^"]*uddg=[^"]+)"[^>]*>(.*?)</a>',
            html,
            flags=re.I | re.S,
        )
    rows = []
    seen = set()
    sub_l = sub.lower()
    for href, title_html in blocks:
        title = re.sub(r"<[^>]+>", "", title_html)
        title = re.sub(r"\s+", " ", title).strip()
        real = href
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            real = urllib.parse.unquote(m.group(1))
        if "reddit.com" not in real.lower():
            continue
        path_m = re.search(r"reddit\.com(/r/[^?#]+)", real, flags=re.I)
        if not path_m:
            continue
        path = path_m.group(1).split("?")[0]
        if f"/r/{sub_l}/" not in path.lower():
            continue
        if path in seen:
            continue
        seen.add(path)
        rows.append(
            {
                "title": title or path,
                "author": "web",
                "permalink": path,
                "created_utc": 0,
                "selftext": "",
                "num_comments": 0,
                "score": 0,
            }
        )
        if len(rows) >= limit:
            break
    if not rows:
        raise RuntimeError("DuckDuckGo returned no Reddit hits")
    return _listing_from_rows(rows)


def _search_web_html(sub: str, q: str, limit: int) -> bytes:
    """Scrape public web search HTML for Reddit links (no API key)."""
    query = f"site:reddit.com/r/{sub} {q}"
    accept_html = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    engines = [
        ("https://html.duckduckgo.com/html/?", "get"),
        ("https://lite.duckduckgo.com/lite/", "post"),
        ("https://search.brave.com/search?", "get"),
        ("https://www.mojeek.com/search?", "get"),
        ("https://www.bing.com/search?", "get"),
    ]
    errors = []
    sub_l = sub.lower()

    for base, mode in engines:
        try:
            if mode == "post":
                html = _post(base, {"q": query}, timeout=30).decode("utf-8", "replace")
            else:
                url = base + urllib.parse.urlencode({"q": query})
                html = _get(url, timeout=30, accept=accept_html).decode("utf-8", "replace")

            # Collect candidate reddit URLs (direct + DDG uddg wrappers)
            hrefs = re.findall(r'href="([^"]+)"', html, flags=re.I)
            rows = []
            seen = set()
            for href in hrefs:
                real = href
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    real = urllib.parse.unquote(m.group(1))
                real = html_unescape(real)
                if "reddit.com" not in real.lower():
                    continue
                path_m = re.search(r"reddit\.com(/r/[^?#\"']+)", real, flags=re.I)
                if not path_m:
                    continue
                path = path_m.group(1).split("?")[0].rstrip("/")
                if f"/r/{sub_l}/" not in path.lower():
                    continue
                if "/comments/" not in path.lower():
                    continue
                # dedupe by post id when possible
                key = path.lower()
                if key in seen:
                    continue
                seen.add(key)
                # title: nearby text is hard; use last path segment
                slug = path.rstrip("/").split("/")[-1].replace("_", " ")
                rows.append(
                    {
                        "title": slug.title() if slug else path,
                        "author": "web",
                        "permalink": path if path.startswith("/") else "/" + path,
                        "created_utc": 0,
                        "selftext": "",
                        "num_comments": 0,
                        "score": 0,
                    }
                )
                if len(rows) >= limit:
                    break

            # Prefer DDG-style titled anchors when present
            titled = None
            try:
                if "result__a" in html or "uddg=" in html:
                    titled = _parse_ddg_html(html, sub, limit)
            except Exception:
                titled = None
            if titled:
                return titled
            if rows:
                return _listing_from_rows(rows)
            raise RuntimeError(f"{base} no Reddit hits")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{base}: {e}")

    raise RuntimeError(" | ".join(errors[-3:]) or "web search failed")


def html_unescape(s: str) -> str:
    return (
        s.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#x2F;", "/")
        .replace("%2F", "/")
    )


def _search_searx(sub: str, q: str, limit: int) -> bytes:
    query = f"site:reddit.com/r/{sub} {q}"
    instances = [
        "https://searx.tiekoetter.com/search",
        "https://search.ononoki.org/search",
        "https://searxng.site/search",
    ]
    last_err: Exception | None = None
    sub_l = sub.lower()
    for base in instances:
        url = base + "?" + urllib.parse.urlencode({"q": query, "format": "json"})
        try:
            raw = _get(url, timeout=25)
            if not raw.lstrip().startswith(b"{"):
                raise RuntimeError("Searx returned non-JSON")
            data = json.loads(raw)
            results = data.get("results") or []
            rows = []
            seen = set()
            for item in results:
                link = item.get("url") or ""
                if f"reddit.com/r/{sub_l}/" not in link.lower():
                    continue
                path_m = re.search(r"reddit\.com(/r/[^?#]+)", link, flags=re.I)
                if not path_m:
                    continue
                path = path_m.group(1).split("?")[0]
                if path in seen:
                    continue
                seen.add(path)
                rows.append(
                    {
                        "title": item.get("title") or path,
                        "author": "web",
                        "permalink": path,
                        "created_utc": 0,
                        "selftext": (item.get("content") or "")[:180],
                        "num_comments": 0,
                        "score": 0,
                    }
                )
                if len(rows) >= limit:
                    break
            if rows:
                return _listing_from_rows(rows)
            raise RuntimeError(f"no hits from {base}")
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err or RuntimeError("Searx failed")


def fetch_reddit_listing(sub: str, q: str, sort: str, limit: str) -> bytes:
    sort_type = {
        "new": "created_utc",
        "top": "score",
        "comments": "num_comments",
        "relevance": "score",
    }.get(sort, "created_utc")
    try:
        lim = max(1, min(50, int(limit)))
    except ValueError:
        lim = 25
    errors: list[str] = []

    # 1) Public web search HTML (DDG / Brave / Mojeek / Bing)
    try:
        return _search_web_html(sub, q, lim)
    except Exception as e:  # noqa: BLE001
        errors.append(f"web: {e}")

    # 2) Public Searx JSON instances
    try:
        return _search_searx(sub, q, lim)
    except Exception as e:  # noqa: BLE001
        errors.append(f"searx: {e}")

    # 3) PullPush mirror
    pp = (
        "https://api.pullpush.io/reddit/search/submission/?"
        + urllib.parse.urlencode(
            {
                "q": q,
                "subreddit": sub,
                "size": str(lim),
                "sort": "desc",
                "sort_type": sort_type,
            }
        )
    )
    try:
        raw = json.loads(_get(pp, timeout=30))
        rows = raw.get("data") if isinstance(raw, dict) else raw
        listing = _listing_from_rows(rows)
        if json.loads(listing)["data"]["children"]:
            return listing
        raise RuntimeError("PullPush empty")
    except Exception as e:  # noqa: BLE001
        errors.append(f"pullpush: {e}")

    # 4) Arctic Shift archive
    arctic = (
        "https://arctic-shift.photon-reddit.com/api/posts/search?"
        + urllib.parse.urlencode(
            {
                "subreddit": sub,
                "query": q,
                "limit": str(lim),
                "sort": "desc",
                "sort_type": sort_type,
            }
        )
    )
    try:
        raw = json.loads(_get(arctic, timeout=45))
        rows = raw.get("data") if isinstance(raw, dict) else raw
        listing = _listing_from_rows(rows)
        if json.loads(listing)["data"]["children"]:
            return listing
        raise RuntimeError("Arctic Shift empty")
    except Exception as e:  # noqa: BLE001
        errors.append(f"arctic: {e}")

    # 5) Direct Reddit JSON (works on some networks)
    params = urllib.parse.urlencode(
        {"q": q, "restrict_sr": "1", "sort": sort, "limit": str(lim), "raw_json": "1"}
    )
    for host in ("www.reddit.com", "old.reddit.com"):
        url = f"https://{host}/r/{urllib.parse.quote(sub)}/search.json?{params}"
        try:
            return _as_reddit_listing(_get(url))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{host}: {e}")

    raise RuntimeError(" · ".join(errors[-4:]) or "fetch failed")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html", "/multi-sub-search.html"):
            if not HTML.exists():
                self._send(404, b"multi-sub-search.html not found next to this script", "text/plain")
                return
            self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/reddit":
            qs = urllib.parse.parse_qs(parsed.query)
            sub = (qs.get("sub") or [""])[0].strip()
            q = (qs.get("q") or [""])[0].strip()
            sort = (qs.get("sort") or ["new"])[0].strip() or "new"
            limit = (qs.get("limit") or ["25"])[0].strip() or "25"
            if not sub or not q:
                self._send(400, b'{"error":"sub and q required"}', "application/json")
                return
            try:
                data = fetch_reddit_listing(sub, q, sort, limit)
                self._send(200, data, "application/json; charset=utf-8")
            except urllib.error.HTTPError as e:
                detail = e.read()[:300].decode("utf-8", "replace")
                msg = json.dumps({"error": f"HTTP {e.code}", "detail": detail}).encode()
                self._send(502, msg, "application/json")
            except Exception as e:  # noqa: BLE001
                msg = json.dumps({"error": str(e)}).encode()
                self._send(502, msg, "application/json")
            return

        self._send(404, b"not found", "text/plain")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Multi-Sub Search running at {url}", flush=True)
    print("Keep this window open. Ctrl+C to stop.", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)


if __name__ == "__main__":
    main()
