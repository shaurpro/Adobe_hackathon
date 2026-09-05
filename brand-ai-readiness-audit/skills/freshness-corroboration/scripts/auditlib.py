"""
auditlib.py — zero-dependency helpers shared by the brand-ai-readiness-audit skills.

Deliberately stdlib-only so every skill folder stays independently portable
(agentskills.io skills must not assume a package manager ran first).

An identical copy of this file ships inside each skill's scripts/ folder.
Nothing here performs a write, a POST, or an authenticated request.
"""

import gzip
import io
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

__all__ = [
    "DEFAULT_UA", "AI_AGENTS", "fetch", "RobotsTxt", "HTMLDoc", "Finding",
    "norm_text", "tokens", "same_registrable_domain", "registrable_domain",
    "url_join", "is_internal", "template_signature", "shingles", "jaccard",
    "SEVERITY_RANK", "clamp_severity", "utc_now",
]

DEFAULT_UA = (
    "Mozilla/5.0 (compatible; BrandAIReadinessAudit/1.0; "
    "+read-only audit bot; respects robots.txt)"
)

# User agents whose access we evaluate against robots.txt. We never impersonate
# them; we only ask "would this site let them in?".
AI_AGENTS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-User",
    "anthropic-ai", "PerplexityBot", "Perplexity-User", "CCBot",
    "Google-Extended", "Googlebot", "Bingbot", "Applebot", "Applebot-Extended",
    "meta-externalagent", "Amazonbot", "Bytespider",
]

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}

# Regions whose text is site chrome, not the fact-bearing body. Excluded from
# "main text" so render-gap and thin-content checks do not fire on boilerplate.
CHROME = {"nav", "header", "footer", "aside", "script", "style", "noscript",
          "svg", "template", "form", "button", "select", "option", "iframe"}

BLOCK = {"p", "div", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
         "section", "article", "main", "blockquote", "pre", "dd", "dt", "figcaption"}


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Capture the redirect chain instead of following it blindly."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        req._chain = getattr(req, "_chain", [])
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, timeout=12, ua=DEFAULT_UA, max_bytes=3_000_000, accept_html=True,
          max_redirects=8):
    """
    GET a URL read-only. Returns a dict; never raises.

    Keys: url, final_url, status, ok, headers (lowercased), body (str),
          raw_len, chain (list of (status, location)), elapsed_ms, error,
          content_type.
    """
    out = {
        "url": url, "final_url": url, "status": None, "ok": False,
        "headers": {}, "body": "", "raw_len": 0, "chain": [],
        "elapsed_ms": None, "error": None, "content_type": "",
    }
    started = time.time()
    current = url
    seen = set()
    try:
        for _ in range(max_redirects + 1):
            if current in seen:
                out["error"] = "redirect_loop"
                break
            seen.add(current)
            req = urllib.request.Request(current, method="GET")
            req.add_header("User-Agent", ua)
            req.add_header(
                "Accept",
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                if accept_html else "*/*")
            req.add_header("Accept-Encoding", "gzip, identity")
            req.add_header("Accept-Language", "en;q=0.9,*;q=0.5")
            ctx = ssl.create_default_context()
            opener = urllib.request.build_opener(
                type("NR", (urllib.request.HTTPRedirectHandler,), {
                    "redirect_request": lambda *a, **k: None})(),
                urllib.request.HTTPSHandler(context=ctx))
            try:
                resp = opener.open(req, timeout=timeout)
                status = resp.getcode()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                raw = resp.read(max_bytes)
            except urllib.error.HTTPError as e:
                status = e.code
                headers = {k.lower(): v for k, v in (e.headers or {}).items()}
                raw = e.read(max_bytes) if e.fp else b""
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                nxt = urllib.parse.urljoin(current, headers["location"])
                out["chain"].append({"status": status, "from": current, "to": nxt})
                current = nxt
                continue
            if headers.get("content-encoding", "").lower() == "gzip":
                try:
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                except Exception:
                    pass
            out["status"] = status
            out["headers"] = headers
            out["final_url"] = current
            out["raw_len"] = len(raw)
            out["content_type"] = headers.get("content-type", "")
            charset = "utf-8"
            m = re.search(r"charset=([\w\-]+)", out["content_type"], re.I)
            if m:
                charset = m.group(1)
            try:
                out["body"] = raw.decode(charset, errors="replace")
            except LookupError:
                out["body"] = raw.decode("utf-8", errors="replace")
            out["ok"] = 200 <= status < 300
            break
        else:
            out["error"] = "too_many_redirects"
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    except Exception as e:  # pragma: no cover - defensive
        out["error"] = f"{type(e).__name__}: {e}"
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    return out


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------

class RobotsTxt:
    """
    Per-user-agent robots.txt parser.

    stdlib's robotparser collapses information we need (which specific UA group
    a rule came from), so we keep the groups intact in order to report
    "GPTBot is blocked but Googlebot is not" with exact evidence.
    """

    def __init__(self, text="", status=None, fetch_error=None):
        self.raw = text or ""
        self.status = status
        self.fetch_error = fetch_error
        self.groups = {}          # ua(lower) -> {"allow": [...], "disallow": [...]}
        self.sitemaps = []
        self.crawl_delay = {}
        self.parse_error = None
        self._parse()

    @property
    def exists(self):
        return self.status == 200 and bool(self.raw.strip())

    @property
    def missing(self):
        # 404 on robots.txt means "everything allowed" — that is valid, not a defect.
        return self.status in (404, 410)

    @property
    def unreachable(self):
        return self.status is None or self.status >= 500 or bool(self.fetch_error)

    def _parse(self):
        current = []
        last_was_ua = False
        for line in self.raw.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field = field.strip().lower()
            value = value.strip()
            if field == "user-agent":
                ua = value.lower()
                if not last_was_ua:
                    current = []
                current.append(ua)
                self.groups.setdefault(ua, {"allow": [], "disallow": []})
                last_was_ua = True
                continue
            last_was_ua = False
            if field == "sitemap" and value:
                self.sitemaps.append(value)
            elif field in ("allow", "disallow") and current:
                for ua in current:
                    self.groups[ua][field].append(value)
            elif field == "crawl-delay" and current:
                for ua in current:
                    try:
                        self.crawl_delay[ua] = float(value)
                    except ValueError:
                        pass

    def group_for(self, ua):
        ua = ua.lower()
        best = None
        for key in self.groups:
            if key == "*":
                continue
            if key and (key in ua or ua in key):
                if best is None or len(key) > len(best):
                    best = key
        if best:
            return best, self.groups[best]
        if "*" in self.groups:
            return "*", self.groups["*"]
        return None, {"allow": [], "disallow": []}

    @staticmethod
    def _match(pattern, path):
        if pattern == "":
            return None
        # Translate robots wildcards (* and $) to regex.
        rx = ""
        for ch in pattern:
            if ch == "*":
                rx += ".*"
            elif ch == "$":
                rx += "$"
            else:
                rx += re.escape(ch)
        try:
            return re.match(rx, path) is not None
        except re.error:
            return path.startswith(pattern.replace("*", ""))

    def allowed(self, ua, path):
        """Longest-match wins, Allow beats Disallow on ties (Google convention)."""
        name, group = self.group_for(ua)
        if not group["allow"] and not group["disallow"]:
            return True, None
        best_len, best_rule, best_allow = -1, None, True
        for kind in ("allow", "disallow"):
            for pattern in group[kind]:
                if kind == "disallow" and pattern == "":
                    continue  # "Disallow:" empty means allow all
                if self._match(pattern, path):
                    plen = len(pattern)
                    if plen > best_len or (plen == best_len and kind == "allow"):
                        best_len, best_rule, best_allow = plen, pattern, kind == "allow"
        if best_rule is None:
            return True, None
        return best_allow, f"{name}: {'Allow' if best_allow else 'Disallow'}: {best_rule}"

    def blanket_block(self, ua):
        """True when this UA is denied the site root."""
        ok, rule = self.allowed(ua, "/")
        return (not ok), rule


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------

class HTMLDoc(HTMLParser):
    """
    Minimal, forgiving HTML reader.

    Records enough structure to answer: what is in the raw bytes, where does
    each piece of text live (chrome vs main), and what machine-readable
    metadata is declared.
    """

    def __init__(self, html, base_url=""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.stack = []
        self.title = ""
        self._in_title = False
        self.meta = []            # list of dicts of raw attrs
        self.links = []           # {href, text, rel, nofollow, absolute}
        self._link_buf = None
        self.headings = []        # {level, text}
        self._heading_buf = None
        self.images = []          # {src, alt, has_alt, w, h, in_chrome}
        self.scripts = []         # {type, src, content}
        self._script_buf = None
        self._script_attrs = None
        self.jsonld_raw = []
        self.noscript_text = []
        self._noscript_depth = 0
        self.main_blocks = []     # text blocks outside chrome
        self.all_blocks = []
        self._buf = []
        self._buf_chrome = False
        self._block_tag = None
        self.tag_counts = {}
        self.microdata_types = []
        self.has_main_landmark = False
        self.body_start_index = None
        self.mount_nodes = []     # empty div#root / #app style SPA shells
        self._open_attrs = []
        self.forms = 0
        self.iframes = []
        self.videos = []
        self.tracks = 0
        self.tables = 0
        self.lang = ""
        self.viewport = ""
        self.parse_ok = True
        try:
            self.feed(html)
            self.close()
        except Exception:
            self.parse_ok = False

    # -- helpers ----------------------------------------------------------
    def _in_chrome(self):
        return any(t in CHROME for t in self.stack)

    def _flush(self):
        text = norm_text("".join(self._buf))
        was_chrome = self._buf_chrome
        self._buf = []
        self._buf_chrome = False
        if not text:
            return
        self.all_blocks.append(text)
        if not was_chrome:
            self.main_blocks.append(text)

    # -- handlers ---------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tag_counts[tag] = self.tag_counts.get(tag, 0) + 1
        if tag in BLOCK or tag in CHROME:
            self._flush()
        if tag not in VOID:
            self.stack.append(tag)
            self._open_attrs.append((tag, a))
        if tag == "html":
            self.lang = a.get("lang", "") or a.get("xml:lang", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            self.meta.append(a)
            if (a.get("name", "").lower() == "viewport"):
                self.viewport = a.get("content", "")
        elif tag == "link":
            self.links.append({
                "href": a.get("href", ""), "rel": (a.get("rel", "") or "").lower(),
                "text": "", "kind": "link", "hreflang": a.get("hreflang", ""),
                "type": a.get("type", ""),
            })
        elif tag == "a":
            self._link_buf = {
                "href": a.get("href", ""), "rel": (a.get("rel", "") or "").lower(),
                "text": "", "kind": "a", "in_chrome": self._in_chrome(),
                "target": a.get("target", ""),
                "aria_label": a.get("aria-label", ""),
            }
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_buf = {"level": int(tag[1]), "text": "",
                                 "in_chrome": self._in_chrome()}
        elif tag == "img":
            self.images.append({
                "src": a.get("src", "") or a.get("data-src", ""),
                "alt": a.get("alt"), "has_alt": "alt" in a,
                "w": a.get("width", ""), "h": a.get("height", ""),
                "in_chrome": self._in_chrome(),
                "loading": a.get("loading", ""),
                "role": a.get("role", ""),
            })
        elif tag == "script":
            self._script_attrs = a
            self._script_buf = []
        elif tag == "noscript":
            self._noscript_depth += 1
        elif tag == "main" or a.get("role") == "main":
            self.has_main_landmark = True
        elif tag == "form":
            self.forms += 1
        elif tag == "iframe":
            self.iframes.append(a.get("src", ""))
        elif tag == "video":
            self.videos.append(a.get("src", ""))
        elif tag == "track":
            self.tracks += 1
        elif tag == "table":
            self.tables += 1
        if a.get("itemtype"):
            self.microdata_types.append(a["itemtype"])
        node_id = (a.get("id") or "").lower()
        if tag == "div" and node_id in ("root", "app", "__next", "___gatsby",
                                        "main-content", "svelte"):
            self.mount_nodes.append({"id": node_id, "text_len": 0,
                                     "start": len(self.all_blocks)})

    def handle_endtag(self, tag):
        if tag in BLOCK or tag in CHROME:
            self._flush()
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._link_buf is not None:
            self._link_buf["text"] = norm_text(self._link_buf["text"])
            self.links.append(self._link_buf)
            self._link_buf = None
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._heading_buf:
            self._heading_buf["text"] = norm_text(self._heading_buf["text"])
            if self._heading_buf["text"]:
                self.headings.append(self._heading_buf)
            self._heading_buf = None
        elif tag == "script" and self._script_buf is not None:
            content = "".join(self._script_buf)
            attrs = self._script_attrs or {}
            stype = (attrs.get("type", "") or "").lower()
            self.scripts.append({"type": stype, "src": attrs.get("src", ""),
                                 "len": len(content),
                                 "async": "async" in attrs,
                                 "defer": "defer" in attrs})
            if "ld+json" in stype:
                self.jsonld_raw.append(content)
            self._script_buf = None
            self._script_attrs = None
        elif tag == "noscript":
            self._noscript_depth = max(0, self._noscript_depth - 1)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == tag:
                del self.stack[i:]
                del self._open_attrs[i:]
                break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._script_buf is not None:
            self._script_buf.append(data)
            return
        if self._noscript_depth:
            t = norm_text(data)
            if t:
                self.noscript_text.append(t)
            return
        if self._link_buf is not None:
            self._link_buf["text"] += data
        if self._heading_buf is not None:
            self._heading_buf["text"] += data
        if any(t in ("script", "style", "svg", "template") for t in self.stack):
            return
        if not self._buf:
            self._buf_chrome = self._in_chrome()
        self._buf.append(data)

    # -- derived views ----------------------------------------------------
    @property
    def main_text(self):
        return " ".join(self.main_blocks)

    @property
    def all_text(self):
        return " ".join(self.all_blocks)

    def meta_value(self, name=None, prop=None):
        for m in self.meta:
            if name and (m.get("name", "").lower() == name.lower()):
                return m.get("content", "")
            if prop and (m.get("property", "").lower() == prop.lower()):
                return m.get("content", "")
        return ""

    def canonical(self):
        for l in self.links:
            if l.get("kind") == "link" and "canonical" in l.get("rel", ""):
                return urllib.parse.urljoin(self.base_url, l["href"]) if l["href"] else ""
        return ""

    def hreflangs(self):
        return [(l.get("hreflang", ""), urllib.parse.urljoin(self.base_url, l["href"]))
                for l in self.links
                if l.get("kind") == "link" and "alternate" in l.get("rel", "")
                and l.get("hreflang")]

    def jsonld(self):
        """Returns (parsed_nodes, errors). Flattens @graph."""
        nodes, errors = [], []
        for i, raw in enumerate(self.jsonld_raw):
            text = raw.strip()
            if not text:
                errors.append({"block": i, "error": "empty block"})
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                snippet = text[:120].replace("\n", " ")
                errors.append({"block": i,
                               "error": f"JSON parse failed at line {e.lineno} col {e.colno}: {e.msg}",
                               "snippet": snippet})
                continue
            for node in _flatten_ld(data):
                node["__block"] = i
                nodes.append(node)
        return nodes, errors

    def anchors(self, internal_only=False):
        res = []
        for l in self.links:
            if l.get("kind") != "a":
                continue
            href = (l.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            absu = urllib.parse.urljoin(self.base_url, href)
            if internal_only and not is_internal(absu, self.base_url):
                continue
            item = dict(l)
            item["absolute"] = absu.split("#")[0]
            res.append(item)
        return res


def _flatten_ld(data):
    out = []
    if isinstance(data, list):
        for d in data:
            out.extend(_flatten_ld(d))
    elif isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            for d in data["@graph"]:
                out.extend(_flatten_ld(d))
            rest = {k: v for k, v in data.items() if k != "@graph"}
            if len(rest) > 1:
                out.append(rest)
        else:
            out.append(data)
    return out


# --------------------------------------------------------------------------
# Text utilities
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")


def norm_text(s):
    return _WS.sub(" ", (s or "")).strip()


def tokens(s):
    return _WORD.findall((s or "").lower())


def shingles(s, n=5):
    t = tokens(s)
    return {" ".join(t[i:i + n]) for i in range(max(0, len(t) - n + 1))}


def jaccard(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def registrable_domain(url):
    """
    Approximate eTLD+1 without the public-suffix list. Handles the common
    two-label suffixes so 'example.co.uk' is not read as 'co.uk'.
    """
    host = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    if re.fullmatch(r"[\d.]+", host) or ":" in host or "." not in host:
        return host  # IP literal, IPv6 or single-label host (e.g. localhost)
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    two = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "mil"}
    if parts[-2] in two and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_registrable_domain(a, b):
    return registrable_domain(a) == registrable_domain(b) and registrable_domain(a) != ""


def is_internal(url, base):
    return same_registrable_domain(url, base)


def url_join(base, href):
    return urllib.parse.urljoin(base, href)


def template_signature(url, doc):
    """
    Cheap template fingerprint: URL shape (digits/slugs collapsed) + heading
    skeleton. Lets the orchestrator roll N page-level findings into one
    template-level finding instead of emitting N near-duplicates.
    """
    path = urllib.parse.urlsplit(url).path
    segs = []
    for s in path.strip("/").split("/"):
        if not s:
            continue
        if re.fullmatch(r"[\d\-_.]+", s):
            segs.append("<num>")
        elif len(s) > 24 or s.count("-") >= 3:
            segs.append("<slug>")
        else:
            segs.append(s.lower())
    shape = "/" + "/".join(segs[:3])
    skel = "".join(str(h["level"]) for h in doc.headings[:8]) if doc else ""
    return f"{shape}|h{skel}"


def clamp_severity(sev, ceiling):
    return sev if SEVERITY_RANK[sev] <= SEVERITY_RANK[ceiling] else ceiling


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

class Finding(dict):
    """
    One audit finding. The extension fields beyond the required report schema
    (mechanism, signal, scope, confidence, evidence_key, supersedes) exist so
    the orchestrator can dedupe and prioritise deterministically rather than
    by string similarity.
    """

    def __init__(self, signal, mechanism, title, severity, evidence,
                 action_summary, action_priority=None, scope="page",
                 confidence="observed", urls=None, evidence_key=None,
                 skill=None, non_deterministic=False, action_detail=None,
                 falsifier=None):
        urls = urls or []
        super().__init__({
            "id": None,
            "title": title,
            "severity": severity,
            "evidence": evidence,
            "suggested_action": {
                "summary": action_summary,
                "priority": action_priority or severity,
                **({"detail": action_detail} if action_detail else {}),
            },
            "mechanism": mechanism,
            "signal": signal,
            "skill": skill,
            "scope": scope,
            "confidence": confidence,
            "affected_urls": urls[:10],
            "affected_count": len(urls),
            "evidence_key": evidence_key or f"{signal}:{scope}",
            "non_deterministic": non_deterministic,
            **({"falsified_by": falsifier} if falsifier else {}),
        })


def emit(findings, meta, path=None):
    payload = {"meta": meta, "findings": findings}
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return payload


# --------------------------------------------------------------------------
# Crawling (shared so the three audit skills fetch a page at most once)
# --------------------------------------------------------------------------

def get_robots(site_url, timeout=10, ua=DEFAULT_UA):
    base = urllib.parse.urlsplit(site_url)
    robots_url = urllib.parse.urlunsplit((base.scheme or "https", base.netloc,
                                          "/robots.txt", "", ""))
    r = fetch(robots_url, timeout=timeout, ua=ua, accept_html=False)
    return RobotsTxt(r["body"] if r["ok"] else "", status=r["status"],
                     fetch_error=r["error"]), robots_url, r


def discover_sitemaps(site_url, robots, timeout=10, ua=DEFAULT_UA, max_urls=500):
    """Return (sitemap_reports, urls). Follows one level of sitemap index."""
    base = urllib.parse.urlsplit(site_url)
    roots = list(dict.fromkeys(robots.sitemaps)) or []
    guessed = []
    if not roots:
        for p in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"):
            guessed.append(urllib.parse.urlunsplit(
                (base.scheme or "https", base.netloc, p, "", "")))
    reports, urls = [], []
    for sm in (roots or guessed):
        r = fetch(sm, timeout=timeout, ua=ua, accept_html=False)
        entry = {"url": sm, "status": r["status"], "declared_in_robots": sm in roots,
                 "error": r["error"], "urls": 0, "lastmod": 0, "children": []}
        if r["ok"]:
            body = r["body"]
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body, re.I)
            lastmods = re.findall(r"<lastmod>", body, re.I)
            entry["lastmod"] = len(lastmods)
            if "<sitemapindex" in body.lower():
                entry["children"] = locs[:20]
                for child in locs[:5]:
                    cr = fetch(child, timeout=timeout, ua=ua, accept_html=False)
                    if cr["ok"]:
                        clocs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", cr["body"], re.I)
                        urls.extend(clocs)
                        entry["urls"] += len(clocs)
            else:
                urls.extend(locs)
                entry["urls"] = len(locs)
        reports.append(entry)
        if entry["status"] == 200:
            break
    seen, dedup = set(), []
    for u in urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            dedup.append(u)
        if len(dedup) >= max_urls:
            break
    return reports, dedup


def crawl(start_url, robots=None, max_pages=20, timeout=12, delay=0.4,
          ua=DEFAULT_UA, seeds=None, budget_seconds=150):
    """
    Breadth-first, same-registrable-domain crawl. Read-only GETs only.

    Skips any URL our own declared UA is disallowed from (guardrail: we obey
    robots.txt even though the audit is about whether *other* agents can get in).
    Returns a list of page records suitable for JSON caching.
    """
    started = time.time()
    queue = [start_url]
    for s in (seeds or []):
        if is_internal(s, start_url) and s not in queue:
            queue.append(s)
    seen, pages = set(), []
    while queue and len(pages) < max_pages:
        if time.time() - started > budget_seconds:
            break
        url = queue.pop(0)
        key = url.split("#")[0].rstrip("/") or url
        if key in seen:
            continue
        seen.add(key)
        path = urllib.parse.urlsplit(url).path or "/"
        if robots is not None:
            allowed, rule = robots.allowed(ua, path)
            if not allowed:
                pages.append({"url": url, "skipped": "robots_disallow",
                              "rule": rule, "status": None, "html": "",
                              "headers": {}, "chain": [], "final_url": url,
                              "elapsed_ms": 0, "error": None, "raw_len": 0,
                              "content_type": ""})
                continue
        r = fetch(url, timeout=timeout, ua=ua)
        is_html = "html" in (r["content_type"] or "").lower() or (
            not r["content_type"] and "<html" in r["body"][:2000].lower())
        rec = {
            "url": url, "final_url": r["final_url"], "status": r["status"],
            "headers": r["headers"], "chain": r["chain"], "error": r["error"],
            "elapsed_ms": r["elapsed_ms"], "raw_len": r["raw_len"],
            "content_type": r["content_type"], "is_html": is_html,
            "html": r["body"][:600_000] if is_html else "",
            "skipped": None, "depth_from_start": 0,
        }
        pages.append(rec)
        if is_html and r["ok"]:
            doc = HTMLDoc(rec["html"], base_url=r["final_url"])
            for a in doc.anchors(internal_only=True):
                u = a["absolute"]
                if u.lower().endswith((".jpg", ".png", ".gif", ".svg", ".zip",
                                       ".mp4", ".webp", ".css", ".js", ".ico")):
                    continue
                k = u.rstrip("/") or u
                if k not in seen and u not in queue and len(queue) < max_pages * 6:
                    queue.append(u)
        time.sleep(delay)
    return pages


def load_pages(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_pages(pages, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(pages, fh)


def html_pages(pages):
    return [p for p in pages if p.get("is_html") and p.get("status") == 200
            and p.get("html")]


def doc_for(page):
    return HTMLDoc(page.get("html", ""), base_url=page.get("final_url") or page.get("url", ""))
