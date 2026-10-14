#!/usr/bin/env python3
"""
AI Competitor Watch Agent  (v2)
===============================
Give it 5-10 companies; it finds recent updates, removes noise and duplicates,
categorizes them, and writes an intelligence brief (HTML + Markdown + JSON).

Sources
  1. News search (Google News RSS)            - broad coverage
  2. Official company feeds (RSS/Atom)        - first-party announcements
  3. Page watcher (pricing pages, newsrooms)  - detects *changes* between runs

Pipeline:  COLLECT -> NOISE FILTER -> DEDUPE -> CLASSIFY/SCORE -> SUMMARY -> RENDER

Quick start
  python competitor_watch.py --check-sources            # verify feeds/pages in sources.json
  python competitor_watch.py "OpenAI" "Anthropic" "Mistral AI" "Cohere" "Perplexity" --open
  python competitor_watch.py --demo --no-llm --open     # offline demo

LLM (optional; falls back to rules if no key):
  GEMINI_API_KEY    free key from aistudio.google.com   (no pip install needed)
  ANTHROPIC_API_KEY paid; needs `pip install anthropic`
Optional: SLACK_WEBHOOK_URL posts the top signals to Slack.
Everything else uses the Python standard library.
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field, asdict
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote_plus, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.environ.get("WATCH_MODEL", "claude-sonnet-5-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
CATEGORIES = ["product", "pricing", "hiring", "partnership", "funding_mna",
              "legal_regulatory", "security_outage", "model_release", "research",
              "company_announcement", "other"]
QUERY_ANGLES = ["", "launch OR release OR announces", "pricing OR plans",
                "hiring OR layoffs OR appoints", "partnership OR integration"]
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ----------------------------------------------------------------------------
# Data model + helpers
# ----------------------------------------------------------------------------
@dataclass
class Item:
    company: str
    title: str
    url: str
    source: str
    published: dt.datetime
    snippet: str = ""
    category: str = "other"
    importance: int = 2            # 1 (trivia) .. 5 (act now)
    summary: str = ""
    relevant: bool = True
    official: bool = False         # first-party (company feed / watched page)
    via: str = "news"              # news | company-feed | page-diff
    also_covered_by: list = field(default_factory=list)
    relevance_label: str = "company_specific"  # company_specific | competitor_context

    def to_dict(self):
        d = asdict(self)
        d["published"] = self.published.isoformat()
        return d


def parse_target(raw: str):
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    return parts[0], parts


def validate_sources_config(cfg: dict):
    """Validate structure and fields of loaded sources configuration."""
    if not isinstance(cfg, dict):
        raise ValueError("Sources configuration must be a JSON object mapping company names to configs.")
    for comp, conf in cfg.items():
        if comp.startswith("_"):
            continue
        if not isinstance(conf, dict):
            raise ValueError(f"Configuration for company '{comp}' must be an object/dict.")
        aliases = conf.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            raise ValueError(f"Aliases for '{comp}' must be a list of strings.")
        feeds = conf.get("feeds", [])
        if not isinstance(feeds, list) or not all(isinstance(u, str) and u.startswith(("http://", "https://")) for u in feeds):
            raise ValueError(f"Feeds for '{comp}' must be a list of valid HTTP(S) URL strings.")
        pages = conf.get("pages", [])
        if not isinstance(pages, list):
            raise ValueError(f"Pages for '{comp}' must be a list of page objects.")
        for pg in pages:
            if not isinstance(pg, dict) or "url" not in pg:
                raise ValueError(f"Each page entry for '{comp}' must be an object with at least a 'url' key.")
            if not isinstance(pg["url"], str) or not pg["url"].startswith(("http://", "https://")):
                raise ValueError(f"Page URL '{pg.get('url')}' in '{comp}' must be a valid HTTP(S) string.")


def load_sources(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
            validate_sources_config(cfg)
            return cfg
    return {}


def build_names(targets, cfg):
    out = {}
    for t in targets:
        name, names = parse_target(t)
        out[name] = list(dict.fromkeys(names + cfg.get(name, {}).get("aliases", [])))
    return out


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        charset = r.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    d = None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def clean_text(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


# ----------------------------------------------------------------------------
# 1a. COLLECT: news search
# ----------------------------------------------------------------------------
def fetch_google_news(company, angle, days):
    q = f'"{company}" {angle}'.strip() + f" when:{days}d"
    url = ("https://news.google.com/rss/search?q=" + quote_plus(q) +
           "&hl=en-US&gl=US&ceid=US:en")
    try:
        root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", http_get(url)).strip())
    except Exception as e:
        print(f"  ! news fetch failed for {q!r}: {e}", file=sys.stderr)
        return []
    items = []
    for it in root.iterfind(".//item"):
        title = html.unescape(it.findtext("title", "")).strip()
        source = (it.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        pub = parse_date(it.findtext("pubDate"))
        if not pub:
            continue
        items.append(Item(company, title, it.findtext("link", ""), source or "unknown",
                          pub, clean_text(it.findtext("description", ""))))
    return items


def collect_news(targets, days):
    out = []
    for raw in targets:
        name, _ = parse_target(raw)
        print(f"News search: {name}")
        for angle in QUERY_ANGLES:
            out.extend(fetch_google_news(name, angle, days))
    return out


# ----------------------------------------------------------------------------
# 1b. COLLECT: official company feeds (RSS or Atom)
# ----------------------------------------------------------------------------
def _local(tag):
    return tag.split("}")[-1]


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def parse_feed(xml_text, company, label):
    xml_text = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_text.lstrip("\ufeff")).strip()
    root = ET.fromstring(xml_text)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        t = _child(el, "title")
        title = clean_text("".join(t.itertext())) if t is not None else ""
        link = ""
        for c in el:
            if _local(c.tag) == "link":
                link = c.get("href") or (c.text or "").strip()
                if link and c.get("rel", "alternate") == "alternate":
                    break
        d = _child(el, "pubDate", "published", "updated", "date")
        pub = parse_date(d.text if d is not None else None)
        sm = _child(el, "description", "summary", "content", "encoded")
        snippet = clean_text("".join(sm.itertext()))[:500] if sm is not None else ""
        if title and link and pub:
            out.append(Item(company, title, link, label, pub, snippet,
                            official=True, via="company-feed"))
    return out


def collect_feeds(targets, cfg):
    out = []
    for raw in targets:
        name, _ = parse_target(raw)
        for url in cfg.get(name, {}).get("feeds", []):
            try:
                got = parse_feed(http_get(url), name, urlparse(url).netloc)
                print(f"Official feed: {name} <- {urlparse(url).netloc}: {len(got)} entries")
                out.extend(got)
            except Exception as e:
                err = str(e)
                if "403" in err:
                    err += " (Cloudflare/bot protection blocks automated client)"
                print(f"  ! feed unavailable {name} ({url}): {err}")
    return out


# ----------------------------------------------------------------------------
# 1c. COLLECT: page watcher (detects changes to pricing pages, newsrooms, ...)
# ----------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}
    BLOCK = {"p", "div", "li", "tr", "td", "th", "br", "section", "article", "header",
             "footer", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "a"}

    def __init__(self):
        super().__init__()
        self.skip, self.buf = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.buf.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)


def page_lines(html_text):
    p = _TextExtractor()
    p.feed(html_text)
    lines = [re.sub(r"\s+", " ", l).strip() for l in "".join(p.buf).split("\n")]
    return [l for l in lines if len(l) > 2]


def watch_pages(targets, cfg, state_dir):
    os.makedirs(state_dir, exist_ok=True)
    items = []
    for raw in targets:
        name, _ = parse_target(raw)
        for pg in cfg.get(name, {}).get("pages", []):
            url, label = pg["url"], pg.get("label", "page")
            cat = pg.get("category", "other")
            try:
                lines = page_lines(http_get(url))
            except Exception as e:
                err_msg = str(e)
                if "403" in err_msg:
                    err_msg += " (Access restricted/Cloudflare: unavailable for automated diff)"
                print(f"  ! page unavailable {name} / {label} ({url}): {err_msg}")
                continue
            if pg.get("ignore_regex"):
                ign = re.compile(pg["ignore_regex"])
                lines = [l for l in lines if not ign.search(l)]
            if len(lines) < 5:
                print(f"  ! page unavailable {name} / {label} ({url}): insufficient static content (JS-rendered) - skipped")
                continue
            path = os.path.join(state_dir, re.sub(r"\W+", "_", name) + "_" +
                                hashlib.sha1(url.encode()).hexdigest()[:8] + ".txt")
            old = None
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    old = f.read().splitlines()
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            if old is None:
                print(f"Page watcher: baseline saved for {name} / {label} (changes show from next run)")
                continue
            old_set, new_set = set(old), set(lines)
            added = [l for l in lines if l not in old_set]
            removed = [l for l in old if l not in new_set]
            if len(added) + len(removed) < pg.get("min_changes", 3):
                continue
            snippet = ("ADDED: " + " | ".join(added[:10])[:350] +
                       "  REMOVED: " + " | ".join(removed[:10])[:250])
            items.append(Item(company=name,
                              title=f"{name} {label} page changed "
                                    f"({len(added)} lines added, {len(removed)} removed)",
                              url=url, source=urlparse(url).netloc,
                              published=dt.datetime.now(dt.timezone.utc), snippet=snippet,
                              category=cat, importance=4 if cat == "pricing" else 3,
                              official=True, via="page-diff"))
            print(f"Page watcher: CHANGE on {name} / {label}")
    return items


def check_sources(targets, cfg):
    """Tell the user which configured URLs actually work."""
    for raw in targets:
        name, _ = parse_target(raw)
        c = cfg.get(name, {})
        if not c.get("feeds") and not c.get("pages"):
            print(f"{name}: (no feeds/pages configured)")
        for url in c.get("feeds", []):
            try:
                got = parse_feed(http_get(url), name, "x")
                newest = max((g.published for g in got), default=None)
                print(f"OK    feed  {name}: {url}  ({len(got)} entries, newest {newest:%Y-%m-%d})"
                      if got else f"WARN  feed  {name}: {url}  (parsed, 0 entries)")
            except Exception as e:
                err = str(e)
                if "403" in err:
                    err += " (Cloudflare/bot protection blocks automated client)"
                print(f"FAIL  feed  {name}: {url}  ({err})")
        for pg in c.get("pages", []):
            try:
                n = len(page_lines(http_get(pg["url"])))
                flag = "OK  " if n >= 20 else "WARN"
                print(f"{flag}  page  {name}: {pg['url']}  ({n} text lines"
                      + ("" if n >= 20 else " - may be JS-rendered, so changes can't be detected") + ")")
            except Exception as e:
                err = str(e)
                if "403" in err:
                    err += " (requires JavaScript / browser challenge)"
                print(f"FAIL  page  {name}: {pg['url']}  ({err})")


# ----------------------------------------------------------------------------
# 2. NOISE FILTER
# ----------------------------------------------------------------------------
NOISE_PATTERNS = re.compile(
    r"(price target|share price|stock (price|forecast|prediction)|should you buy|buy or sell|"
    r"analyst (rating|upgrade|downgrade)|shares? (rise|fall|jump|slide|surge|drop|rally|zoom|climb)s?|"
    r"\btop \d+\b|\bbest \d+\b|\bvs\.? [^ ]+ stock|coupon|promo code|sponsored|"
    r"horoscope|giveaway|\bmarket (size|report) \d{4}|CAGR|press release distribution)", re.I)
LOW_QUALITY_SOURCES = {"msn", "yahoo entertainment", "benzinga", "marketbeat",
                       "tipranks", "investing.com", "motley fool", "seeking alpha",
                       "zacks", "gurufocus", "openpr", "globenewswire - market research",
                       "simply wall street", "tradingview", "pluang", "crypto briefing",
                       "tradingkey", "stock titan", "marketscreener"}


def mentions_company(item, names):
    hay = (item.title + " " + item.snippet).lower()
    return any(n.lower() in hay for n in names)


def noise_filter(items, names_by_company, days):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    kept, dropped = [], 0
    for it in items:
        if it.published < cutoff:
            dropped += 1
        elif it.official:                       # first-party sources skip topical filters
            kept.append(it)
        elif (NOISE_PATTERNS.search(it.title) or it.source.lower() in LOW_QUALITY_SOURCES
              or not mentions_company(it, names_by_company.get(it.company, [it.company]))):
            dropped += 1
        else:
            kept.append(it)
    print(f"Noise filter: kept {len(kept)}, dropped {dropped}")
    return kept


# ----------------------------------------------------------------------------
# 3. DEDUPE (cross-company; official versions win as the canonical item)
# ----------------------------------------------------------------------------
STOP = set("a an the of to in on for and or with by at as from is are its it's new says said "
           "will after over into than this that be has have announces announced".split())


def tokens(title):
    return {w for w in re.findall(r"[a-z0-9\.]+", title.lower()) if w not in STOP and len(w) > 1}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


EXTERNAL_AI_ACTORS = {
    "Meta": ["Meta", "Llama", "Zuckerberg", "Muse"],
    "Google": ["Google", "Gemini", "DeepMind", "Sundar"],
    "Microsoft": ["Microsoft", "Copilot", "Nadella"],
    "Apple": ["Apple", "Apple Intelligence"],
    "Amazon": ["Amazon", "AWS", "Bedrock"],
    "xAI": ["xAI", "Grok", "Elon Musk"],
}


def owner_company(item, names_by_company, default):
    """Determine the true primary subject of a story.
    If the primary subject is an external actor (e.g. Meta, Google, xAI) that merely
    compares against or challenges a monitored target, label it as competitor_context.
    """
    title = item.title
    low = title.lower()

    # Check position of monitored target mentions
    target_matches = []
    for comp, names in names_by_company.items():
        for n in names:
            m = re.search(r"\b" + re.escape(n.lower()) + r"\b", low)
            if m:
                target_matches.append((m.start(), comp, n))

    # Check position of external actor mentions
    external_matches = []
    for ext_name, aliases in EXTERNAL_AI_ACTORS.items():
        for a in aliases:
            m = re.search(r"\b" + re.escape(a.lower()) + r"\b", low)
            if m:
                external_matches.append((m.start(), ext_name, a))

    target_matches.sort(key=lambda x: x[0])
    external_matches.sort(key=lambda x: x[0])

    # If an external actor is the opening subject / leading subject before the monitored target
    # (e.g. "Meta releases Muse to challenge OpenAI" or "Google launches Gemini 4 undercutting Anthropic")
    if external_matches and (not target_matches or external_matches[0][0] < target_matches[0][0]):
        item.relevance_label = "competitor_context"
        # Keep under target if relevant comparison, but ensure relevance_label is clear
        best_target = target_matches[0][1] if target_matches else default
        return best_target

    item.relevance_label = "company_specific"
    return target_matches[0][1] if target_matches else default


def canonical_url(url: str) -> str:
    """Normalize URL by stripping tracking parameters, query fragments, and trailing slashes."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        # Drop standard UTM and click tracking parameters
        clean_q = [
            (k, v) for k, v in [pair.split("=", 1) if "=" in pair else (pair, "")
                                for pair in p.query.split("&") if pair]
            if not k.lower().startswith("utm_") and k.lower() not in ("ref", "source", "ocid", "fbclid", "gclid")
        ]
        q_str = "&".join(f"{k}={v}" if v else k for k, v in clean_q)
        netloc = p.netloc.lower()
        path = p.path.rstrip("/") if len(p.path) > 1 else p.path
        return f"{p.scheme}://{netloc}{path}{'?' + q_str if q_str else ''}"
    except Exception:
        return url


def dedupe(items, names_by_company, threshold=0.4):
    items = sorted(items, key=lambda i: i.published)
    brand = {w for ns in names_by_company.values() for n in ns for w in tokens(n)}
    clusters = []                                          # each: [tokens, canonical_item, canon_url]
    for it in items:
        t = tokens(it.title)
        it_c_url = canonical_url(it.url)
        for cl in clusters:
            canon, canon_c_url = cl[1], cl[2]
            j = jaccard(t, cl[0])
            shared = len((t & cl[0]) - brand)
            if (it_c_url and canon_c_url and it_c_url == canon_c_url) or it.url == canon.url or j >= threshold or (shared >= 4 and j >= 0.3):
                if it.official and not canon.official:      # prefer the first-party item
                    it.also_covered_by = [s for s in dict.fromkeys(
                        canon.also_covered_by + [canon.source]) if s != it.source]
                    cl[0], cl[1], cl[2] = t, it, it_c_url
                elif it.source != canon.source and it.source not in canon.also_covered_by:
                    canon.also_covered_by.append(it.source)
                break
        else:
            clusters.append([t, it, it_c_url])
    result = [cl[1] for cl in clusters]
    for c in result:
        if c.via != "page-diff":
            c.company = owner_company(c, names_by_company, c.company)
    print(f"Dedupe: {len(items)} -> {len(result)} unique stories")
    return result


# ----------------------------------------------------------------------------
# LLM providers: Gemini (free key) or Anthropic (paid)
# ----------------------------------------------------------------------------
def llm_available():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GEMINI_API_KEY"))


def _gemini(system, user, max_tokens, json_mode):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    gen = {"maxOutputTokens": max(max_tokens, 8000), "temperature": 0.2}
    if json_mode:
        gen["responseMimeType"] = "application/json"
    body = json.dumps({"systemInstruction": {"parts": [{"text": system}]},
                       "contents": [{"role": "user", "parts": [{"text": user}]}],
                       "generationConfig": gen}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
            parts = data["candidates"][0]["content"]["parts"]
            time.sleep(6)                       # stay under free-tier requests/minute
            return "".join(p.get("text", "") for p in parts)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 503) and attempt < 3:
                print(f"  ... Gemini busy/rate-limited ({e.code}), waiting 25s")
                time.sleep(25)
                continue
            raise RuntimeError(f"Gemini HTTP {e.code}: {detail}")


def llm_text(system, user, max_tokens=3000, json_mode=False):
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        resp = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in resp.content if b.type == "text")
    return _gemini(system, user, max_tokens, json_mode)


def parse_json(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    return json.loads(text)


# ----------------------------------------------------------------------------
# 4. CLASSIFY + SCORE
# ----------------------------------------------------------------------------
RULES = [
    # ── security / outage ──────────────────────────────────────────────────────
    ("security_outage", (
        r"breach|vulnerab|outage|hack|leak|down for|incident|prompt injection|zero[- ]day|compromis"
        r"|listening device|arrested|charged\b|covert recording|surveillance"
        r"|goes? rogue|exploit\w*\b.{0,40}(?:site|web|gov|agency)|cuts? (?:off )?(?:live )?internet access"
        r"|distillation attack|unintended.{0,20}action|misbehav"
    )),
    # ── legal / regulatory ─────────────────────────────────────────────────────
    ("legal_regulatory", (
        r"lawsuit|sues|sued|antitrust|regulator|probe|fine[sd]?\b|ban(s|ned)?\b|settle|ftc\b"
        r"|eu commission|compliance|copyright|subpoena|investigat|rehearing|appeal|blacklisting|pentagon"
        r"|urges? court|scraps?\b.{0,30}(?:ai|agreement|deal)|terms of service|tos\b"
        r"|junk\b.{0,20}(?:debt|credit)|investment grade"
    )),
    # ── pricing ────────────────────────────────────────────────────────────────
    ("pricing", (
        r"pric(e|es|ing)\b|subscription|tiers?\b|per (month|seat|million|token)|free (tier|plan)"
        r"|discount|cheaper|rate limits?|plan (prices?|changes?)|rates? on api"
        r"|cuts? (?:model )?costs?|halves? (?:model )?costs?|costs? \d+% less"
        r"|\$[\d.]+\s*/\s*[mk] tokens?|cuts? costs? \d+x|\d+x (?:cheaper|less)"
        r"|revenue\b.{0,30}(?:gap|miss|confusion|lighter|below|estimate)"
        r"|annualized revenue|revenue figures?\b"
    )),
    # ── hiring / leadership ────────────────────────────────────────────────────
    ("hiring", (
        r"hiring|hires|hired|appoints?|names? .* (ceo|cto|cfo|chief|head|lead)"
        r"|layoffs?|job cuts|steps down|resign|poach|recruit|headcount|joins as|creates \d+ jobs"
        r"|joins.{0,25}(?:team|go-to-market|safety|research|leadership)"
        r"|fired\b.{0,30}(?:employee|staff|engineer|lead)|trump.{0,20}alumni"
    )),
    # ── partnership / integration ──────────────────────────────────────────────
    ("partnership", (
        r"\b(?:partner|partners|partnership|alliance|teams? up|joins forces|deal with"
        r"|collaborates? with|collaborating with|collaboration with|commitment with"
        r"|signs a pact|brings .* into)\b"
        r"|joins.{0,50}(?:program|defense|initiative)"
        r"|integration with|adds .{0,30} (?:agents?|models?|support)\b"
    )),
    # ── funding / M&A ─────────────────────────────────────────────────────────
    ("funding_mna", (
        r"raises|funding|series [a-f]\b|valuation|acquir|acquisition|merger|ipo\b"
        r"|invests?\b|financing|round led by|annualized revenues?|plans to spend \$\d+"
        r"|data cent(?:er|re).{0,30}(?:project|build|sprint|deal|\$)"
        r"|build.{0,30}data cent(?:er|re)|final talks.{0,30}(?:build|data|deal)"
        r"|\$\d+[bm].{0,20}(?:data cent|deal|project|pitch|raise|investment)"
        r"|\$\d+ (?:trillion|billion|million).{0,20}(?:pitch|deal|raise|investment|project|data)"
        r"|revenue.{0,20}(?:\$\d+[bm]|\d+ billion)|ipo.{0,15}(?:appetite|prospect|pitch)"
        r"|spending \$\d+|commits? \$\d+"
        r"|public prospectus|ipo prospectus"
    )),
    # ── research / safety ─────────────────────────────────────────────────────
    ("research", (
        r"\b(?:published? research|researchers?|arxiv|speculative decoding|dictionary learning"
        r"|system card|evaluat\w* on|benchmark|safety report|research paper"
        r"|self-replicate|alignment|delusions|watermarks?)\b"
        r"|proofs?\b.{0,30}(?:math|ai|generat)|manuscript|mathematical\b"
        r"|alignment failure|safety.{0,15}(?:concern|test|eval|measure|guardrail)"
        r"|consciousness\b|morals?\b.{0,15}ai|ai.{0,15}morals?"
    )),
    # ── model release ─────────────────────────────────────────────────────────
    ("model_release", (
        r"\b(?:gpt-[3456]|gpt-4o|gpt-rosalind|claude|gemini|llama|command r|mistral|voxtral"
        r"|codestral|pixtral|shieldstral|haiku|sonnet|opus|muse)\b"
        r".*\b(?:model|weights|reasoning|voice|vision|multilingual|capabilities|parameters"
        r"|could launch|set to launch|expected to launch|to debut)\b"
        r"|\b(?:model|weights|foundation model)\b.*(?:release|launch|unveil|debut)"
        r"|\b(?:introduces|releases|launches|unveils|announces)\b.*(?:model|reasoning model|foundation model|weights)"
        r"|\blarge \d+\b.{0,20}(?:parameter|token|context|model|launch|trillion)"
        r"|\b\d+ trillion parameters?\b"
        r"|\bopus \d+\b|\bhaiku \d+(?:\.\d+)?\b|\bsonnet \d+(?:\.\d+)?\b"
    )),
    # ── product ───────────────────────────────────────────────────────────────
    ("product", (
        r"\b(?:launch(?:es|ing)?|releases?|unveil\w*|introduc\w*|rolls? out|debuts?)\b"
        r".*(?:feature|search|workspace|product|app|version|console|spaces|assistant|dashboard|scanner)"
        r"|\bnew (?:feature|workspace|product|app|version|console|spaces|search|assistant)\b"
        r"|\b(?:open[- ]sources?|beta\b|sidebar integration)"
        r"|dashboard\b|canvas\b.{0,20}(?:chatgpt|claude|ai)|oss scanner"
        r"|delegate.{0,20}(?:work|task).{0,20}(?:agent|chatgpt|ai)"
        r"|for (?:teens?|financial services|enterprise).{0,30}(?:chatgpt|claude|ai|tool)"
        r"|(?:chatgpt|claude|ai).{0,15}for (?:financial services|enterprise|teens?)\b"
        r"|agentic ai|interactive tools?\b"
    )),
    # ── company announcement ──────────────────────────────────────────────────
    ("company_announcement", (
        r"\b(?:announc\w*|expands?|expansion|headquarters|office|conference|keynote|devday|summit"
        r"|letter|statement|policy|milestone|applied ai lab|takes ai video|turns days of work|turns creative ideas)\b"
        r"|planning for.{0,30}(?:catastroph|crisis|disaster)|catastroph\w+.{0,30}(?:ai|plan|scenario)"
        r"|cruelty to|cruel.{0,20}(?:claude|ai)|prohibits?.{0,20}(?:cruel|abuse)"
        r"|mean to (?:claude|ai)\b|asks users to stop|users can.{0,5}t be"
        r"|new plan.{0,30}(?:protect|critical|infrastructure)|plan to protect"
        r"|history,.{0,15}controversies?\b"
    )),
]


def heuristic_classify(it):
    if it.via == "page-diff":
        it.importance = max(it.importance, 3)
        it.summary = it.title
        return
    text = (it.title + " " + it.snippet).lower()
    title_low = it.title.lower()

    # Keep genuinely ambiguous comparisons, reviews, and opinion pieces as 'other'
    if re.search(r"\b(?:which (?:ai|assistant|model|agent) is right|which (?:is|ai) (?:best|better)|head-to-head\b|showdown\b|opinion:\b|editorial:\b)", title_low):
        it.category = "other"
        it.importance = 2
        it.summary = it.title
        return

    for cat, pat in RULES:
        if re.search(pat, title_low, re.I) or re.search(pat, text, re.I):
            it.category = cat
            break
    base = {"security_outage": 4, "legal_regulatory": 4, "funding_mna": 4, "pricing": 4,
            "model_release": 4, "product": 3, "partnership": 3, "hiring": 3,
            "research": 3, "company_announcement": 3, "other": 2}.get(it.category, 2)
    bonus = (1 if len(it.also_covered_by) >= 2 else 0) + (1 if it.official else 0)
    it.importance = min(5, base + bonus)
    if getattr(it, "relevance_label", "") == "competitor_context":
        it.importance = min(4, it.importance)
    it.summary = it.title


def llm_classify(items, batch=20):
    system = (
        "You are a competitive-intelligence analyst. For each item decide whether it is a "
        "MEANINGFUL business update about the named company (model release, product launch/change, pricing, "
        "research/technical breakthrough, company announcement, hiring/leadership/layoffs, partnership, "
        "funding/M&A, legal/regulatory, security/outage). "
        "Mark relevant=false for: opinion pieces, stock chatter, listicles, rehashed old news, "
        "passing mentions, or items not actually about that company. Items with official=true are "
        "first-party posts: usually relevant. Items with via='page-diff' are detected changes to a "
        "company web page: mark relevant only if the ADDED/REMOVED lines show a real change (prices, "
        "plans, products, announcements), not navigation, dates, cookie banners or layout noise; "
        "in that case summary should state what changed.\n"
        f"category must be one of {CATEGORIES}.\n"
        "importance: 1=trivia, 2=minor, 3=notable, 4=significant to a competitor, 5=strategic/urgent.\n"
        "summary: one factual sentence (<=30 words), no hype. "
        "Return ONLY a JSON array: [{\"id\":int,\"relevant\":bool,\"category\":str,"
        "\"importance\":int,\"summary\":str}] with one object per input id.")
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        payload = [{"id": i, "company": it.company, "title": it.title, "source": it.source,
                    "official": it.official, "via": it.via, "snippet": it.snippet[:400],
                    "also_covered_by": it.also_covered_by[:4]}
                   for i, it in enumerate(chunk)]
        rows = parse_json(llm_text(system, json.dumps(payload), 4000, json_mode=True))
        for row in rows:
            it = chunk[row["id"]]
            it.relevant = bool(row.get("relevant", True))
            it.category = row.get("category") if row.get("category") in CATEGORIES else it.category
            it.importance = max(1, min(5, int(row.get("importance", it.importance))))
            it.summary = row.get("summary") or it.title


def classify(items, use_llm=True):
    for it in items:
        heuristic_classify(it)
    if use_llm and llm_available():
        try:
            llm_classify(items)
            print("Classified with LLM (" +
                  ("Anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "Gemini " + GEMINI_MODEL) + ")")
        except Exception as e:
            print(f"  ! LLM classification failed ({e}); using heuristics", file=sys.stderr)
    else:
        print("Classified with heuristics (no API key / --no-llm)")
    return [i for i in items if i.relevant]


# ----------------------------------------------------------------------------
# 5. SUMMARY (LLM writes only the words; links/facts come from structured data)
# ----------------------------------------------------------------------------
def rank(items):
    return sorted(items, key=lambda i: (-i.importance, not i.official, -i.published.timestamp()))


def select_top_signals(ranked_items, limit=5):
    """Select top signals prioritizing diversity across companies with highest importance."""
    if not ranked_items:
        return []
    selected = []
    seen_companies = set()
    for it in ranked_items:
        if it.company not in seen_companies:
            selected.append(it)
            seen_companies.add(it.company)
            if len(selected) >= limit:
                break
    if len(selected) < limit:
        for it in ranked_items:
            if it not in selected:
                selected.append(it)
                if len(selected) >= limit:
                    break
    return selected


def fallback_summary(items):
    ranked = rank(items)
    per, cats = Counter(i.company for i in items), Counter(i.category for i in items)
    top_items = select_top_signals(ranked, limit=5)
    if items:
        top_co, n_co = per.most_common(1)[0]
        top_cat, n_cat = cats.most_common(1)[0]
        bottom = (f"{len(items)} meaningful updates across {len(per)} companies. "
                  f"Most active: {top_co} ({n_co}). Most common type: {top_cat.replace('_', ' ')} "
                  f"({n_cat}). Ranked with diversity across competitors.")
        watch_next = []
        model_items = [i for i in items if i.category == "model_release"]
        if model_items:
            cos = ", ".join(dict.fromkeys(i.company for i in model_items[:3]))
            watch_next.append(f"Evaluate benchmark evals and developer adoption for new models from {cos}.")
        pricing_items = [i for i in items if i.category == "pricing"]
        if pricing_items:
            cos = ", ".join(dict.fromkeys(i.company for i in pricing_items[:3]))
            watch_next.append(f"Monitor competitive reactions to pricing adjustments by {cos}.")
        prod_items = [i for i in items if i.category == "product"]
        if prod_items:
            cos = ", ".join(dict.fromkeys(i.company for i in prod_items[:3]))
            watch_next.append(f"Track customer feedback and product rollouts from {cos}.")
        research_items = [i for i in items if i.category == "research"]
        if research_items and len(watch_next) < 3:
            cos = ", ".join(dict.fromkeys(i.company for i in research_items[:2]))
            watch_next.append(f"Review technical papers and safety research published by {cos}.")
        if not watch_next:
            watch_next.append("Track subsequent official announcements and changelogs in the upcoming weekly cycle.")
    else:
        bottom = "No meaningful updates found in this window."
        watch_next = []
    return {"bottom_line": bottom,
            "top_signals": [{"item": i, "so_what": ""} for i in top_items],
            "watch_next": watch_next}


def llm_summary(items):
    ranked = rank(items)[:50]
    data = [{"id": n, "company": i.company, "category": i.category, "importance": i.importance,
             "date": i.published.strftime("%Y-%m-%d"), "summary": i.summary,
             "official": i.official, "outlets": 1 + len(i.also_covered_by)}
            for n, i in enumerate(ranked)]
    system = ("You are a senior competitive-intelligence analyst writing for executives. "
              "Use ONLY the facts in the data. Never invent numbers, names or dates.")
    user = ("From these updates, return ONLY JSON: "
            '{"bottom_line": "<=3 sentences on what changed in the landscape", '
            '"top_signals": [{"id": int, "so_what": "one sentence on the implication for a competitor"}], '
            '"watch_next": ["2-3 short things to monitor next week"]}. '
            "Pick at most 5 top_signals, ranked by strategic importance, and prefer diverse companies "
            "and categories over several near-duplicates.\n\nDATA:\n" + json.dumps(data))
    obj = parse_json(llm_text(system, user, 2000, json_mode=True))
    top = []
    for t in obj.get("top_signals", [])[:5]:
        n = t.get("id")
        if isinstance(n, int) and 0 <= n < len(ranked):
            top.append({"item": ranked[n], "so_what": str(t.get("so_what", "")).strip()})
    if not top:
        raise ValueError("LLM returned no usable top_signals")
    return {"bottom_line": str(obj.get("bottom_line", "")).strip(),
            "top_signals": top,
            "watch_next": [str(x) for x in obj.get("watch_next", [])][:3]}


def make_summary(items, use_llm=True):
    if use_llm and llm_available() and items:
        try:
            return llm_summary(items)
        except Exception as e:
            print(f"  ! LLM summary failed ({e}); using rule-based summary")
    return fallback_summary(items)


# ----------------------------------------------------------------------------
# 6. RENDER (Markdown + HTML)
# ----------------------------------------------------------------------------
def tag_of(it):
    parts = []
    if it.via == "page-diff":
        parts.append("(page change)")
    elif it.official:
        parts.append("(official)")
    if getattr(it, "relevance_label", "") == "competitor_context":
        parts.append("(competitor context)")
    return (" " + " ".join(parts)) if parts else ""


def render_markdown(items, summary, targets, days, stats):
    names = [parse_target(t)[0] for t in targets]
    L = ["# Competitor Intelligence Brief",
         f"_{dt.date.today():%d %b %Y} · last {days} days · {', '.join(names)} · {len(items)} updates_",
         "", "## Bottom line", summary["bottom_line"], "", "## Top signals"]
    for n, t in enumerate(summary["top_signals"], 1):
        it = t["item"]
        L.append(f"{n}. **{it.company}** — {it.summary} ([{it.source}]({it.url})){tag_of(it)}")
        if t["so_what"]:
            L.append(f"   - *So what:* {t['so_what']}")
    if summary["watch_next"]:
        L += ["", "## Watch next"] + [f"- {w}" for w in summary["watch_next"]]
    L += ["", "## By company"]
    ranked = rank(items)
    for nme in names:
        mine = [i for i in ranked if i.company == nme]
        L.append(f"\n### {nme} ({len(mine)})")
        if not mine:
            L.append("_No meaningful updates._")
        else:
            primary = mine[:10]
            for it in primary:
                extra = f" +{len(it.also_covered_by)} outlets" if it.also_covered_by else ""
                L.append(f"- `{it.category}` {'★' * it.importance} {it.published:%d %b} — "
                         f"{it.summary} ([{it.source}]({it.url})){tag_of(it)}{extra}")
            if len(mine) > 10:
                L.append(f"\n_... and {len(mine) - 10} additional updates recorded in updates.json._")
    L += ["", "---",
          f"_Pipeline: {stats.get('raw', '?')} raw → {stats.get('after_noise', '?')} after noise filter → "
          f"{stats.get('unique', '?')} unique → {len(items)} meaningful._"]
    return "\n".join(L)


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1a1a1a;--mute:#6b6b6b;--line:#e4e4e0;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--card:#1e1e1e;--ink:#ececec;--mute:#9a9a9a;--line:#2e2e2e;--accent:#6ea0ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:920px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 12px}
.meta{color:var(--mute);margin:0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:24px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi b{display:block;font-size:26px}.kpi span{color:var(--mute);font-size:13px}
.bottom{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:16px 18px;margin:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:10px 0}
.card .so{color:var(--mute);margin:8px 0 0;font-size:15px}.card .so b{color:var(--ink)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.chip{display:inline-block;font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px;
 background:color-mix(in srgb,var(--c) 14%,transparent);color:var(--c);margin-right:6px;white-space:nowrap}
.c-product{--c:#2563eb}.c-pricing{--c:#15803d}.c-hiring{--c:#7c3aed}.c-partnership{--c:#0e7490}
.c-funding_mna{--c:#b45309}.c-legal_regulatory{--c:#b91c1c}.c-security_outage{--c:#c2410c}
.c-model_release{--c:#4338ca}.c-research{--c:#0891b2}.c-company_announcement{--c:#475569}.c-other{--c:#6b7280}
.dots{font-size:11px;letter-spacing:1px;color:var(--mute);white-space:nowrap}
.badge{font-size:11px;border:1px solid var(--line);border-radius:6px;padding:1px 6px;color:var(--mute);margin-left:6px;white-space:nowrap}
.b-ctx{color:#9333ea;border-color:#9333ea40}
details{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:10px 0;padding:4px 16px}
summary{cursor:pointer;font-weight:600;padding:10px 0}summary small{color:var(--mute);font-weight:400;margin-left:8px}
ul.items{list-style:none;margin:0 0 10px;padding:0}ul.items li{padding:9px 0;border-top:1px solid var(--line)}
.date{color:var(--mute);font-size:13px;margin-right:6px}
footer{margin-top:40px;color:var(--mute);font-size:13px}
@media print{body{background:#fff}details{break-inside:avoid}}
"""


def _chip(cat):
    return f'<span class="chip c-{cat}">{html.escape(cat.replace("_", " "))}</span>'


def _badges(it):
    b = ""
    if it.via == "page-diff":
        b += '<span class="badge">page change</span>'
    elif it.official:
        b += '<span class="badge">official</span>'
    if getattr(it, "relevance_label", "") == "competitor_context":
        b += '<span class="badge b-ctx">competitor context</span>'
    if it.also_covered_by:
        b += f'<span class="badge">+{len(it.also_covered_by)} outlets</span>'
    return b


def _link(it):
    return (f'<a href="{html.escape(it.url, quote=True)}" target="_blank" rel="noopener">'
            f'{html.escape(it.summary)}</a>')


def render_html(items, summary, targets, days, stats):
    e = html.escape
    names = [parse_target(t)[0] for t in targets]
    ranked = rank(items)
    high = sum(1 for i in items if i.importance >= 4)
    off = sum(1 for i in items if i.official)
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           f"<title>Competitor Brief - {dt.date.today():%d %b %Y}</title><style>{CSS}</style></head>",
           "<body><main>",
           "<h1>Competitor Intelligence Brief</h1>",
           f'<p class="meta">{dt.date.today():%d %b %Y} · last {days} days · {e(", ".join(names))}</p>',
           '<div class="kpis">'
           f'<div class="kpi"><b>{len(items)}</b><span>meaningful updates</span></div>'
           f'<div class="kpi"><b>{high}</b><span>high importance (4-5)</span></div>'
           f'<div class="kpi"><b>{off}</b><span>first-party sources</span></div>'
           f'<div class="kpi"><b>{len(names)}</b><span>companies watched</span></div></div>',
           f'<h2>Bottom line</h2><p class="bottom">{e(summary["bottom_line"])}</p>',
           "<h2>Top signals</h2>"]
    for it_ in summary["top_signals"]:
        it = it_["item"]
        so = f'<p class="so"><b>So what:</b> {e(it_["so_what"])}</p>' if it_["so_what"] else ""
        out.append(f'<div class="card">{_chip(it.category)}<b>{e(it.company)}</b>'
                   f'<span class="dots"> {"●" * it.importance}{"○" * (5 - it.importance)}</span>'
                   f'{_badges(it)}<br>{_link(it)} <span class="date">— {e(it.source)}, '
                   f'{it.published:%d %b}</span>{so}</div>')
    if summary["watch_next"]:
        out.append("<h2>Watch next</h2><ul>" +
                   "".join(f"<li>{e(w)}</li>" for w in summary["watch_next"]) + "</ul>")
    out.append("<h2>By company</h2>")
    for nme in names:
        mine = [i for i in ranked if i.company == nme]
        out.append(f'<details open><summary>{e(nme)}<small>{len(mine)} updates</small></summary>'
                   '<ul class="items">')
        if not mine:
            out.append('<li class="date">No meaningful updates in this window.</li>')
        else:
            primary = mine[:8]
            for it in primary:
                out.append(f'<li>{_chip(it.category)}<span class="dots">{"●" * it.importance}'
                           f'{"○" * (5 - it.importance)}</span> <span class="date">{it.published:%d %b}'
                           f'</span>{_link(it)} <span class="date">— {e(it.source)}</span>{_badges(it)}</li>')
            if len(mine) > 8:
                remainder = mine[8:]
                out.append('</ul><details style="margin:4px 0 10px"><summary><small>'
                           f'View {len(remainder)} more updates from {e(nme)}</small></summary><ul class="items">')
                for it in remainder:
                    out.append(f'<li>{_chip(it.category)}<span class="dots">{"●" * it.importance}'
                               f'{"○" * (5 - it.importance)}</span> <span class="date">{it.published:%d %b}'
                               f'</span>{_link(it)} <span class="date">— {e(it.source)}</span>{_badges(it)}</li>')
        out.append("</ul></details>")
    cats = Counter(i.category for i in items)
    out.append("<h2>By category</h2><p>" +
               " ".join(f'{_chip(c)}<span class="date">{n}</span>' for c, n in cats.most_common()) + "</p>")
    out.append(f'<footer>Pipeline: {stats.get("raw", "?")} raw items → {stats.get("after_noise", "?")} '
               f'after noise filter → {stats.get("unique", "?")} unique stories → {len(items)} meaningful. '
               'Always verify key claims at the linked source.</footer></main></body></html>')
    return "".join(out)


# ----------------------------------------------------------------------------
# Delivery
# ----------------------------------------------------------------------------
def post_slack(webhook, summary):
    lines = [f"*Competitor brief - {dt.date.today():%d %b %Y}*", summary["bottom_line"], ""]
    for t in summary["top_signals"][:5]:
        it = t["item"]
        lines.append(f"• *{it.company}*: {it.summary} <{it.url}|{it.source}>")
        if t["so_what"]:
            lines.append(f"   _So what:_ {t['so_what']}")
    req = urllib.request.Request(webhook, data=json.dumps({"text": "\n".join(lines)}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


# ----------------------------------------------------------------------------
# Demo data (offline)
# ----------------------------------------------------------------------------
def demo_items():
    now = dt.datetime.now(dt.timezone.utc)
    d = lambda days: now - dt.timedelta(days=days)
    rows = [
        ("OpenAI", "OpenAI launches new reasoning model for enterprise API users", "TechCrunch", 1),
        ("OpenAI", "OpenAI launches new reasoning model for enterprise API customers", "The Verge", 1),
        ("OpenAI", "OpenAI stock price target raised by analyst", "MarketBeat", 1),
        ("Anthropic", "Anthropic cuts API pricing for batch workloads by 30%", "VentureBeat", 2),
        ("Anthropic", "Anthropic partners with Deloitte on enterprise deployments", "Reuters", 3),
        ("Anthropic", "Anthropic hires former Google exec as head of international expansion", "The Information", 4),
        ("Mistral AI", "Mistral AI raises new funding round at higher valuation", "Bloomberg", 3),
        ("Cohere", "Cohere sued over alleged copyright infringement by publishers", "Wired", 5),
    ]
    items = [Item(c, t, f"https://example.com/{i}", s, d(days), snippet=t)
             for i, (c, t, s, days) in enumerate(rows)]
    items.append(Item("OpenAI", "Introducing GPT-6 Sol and Luna", "https://openai.com/news/gpt-6",
                      "openai.com", d(2), "Official announcement", official=True, via="company-feed"))
    items.append(Item("Anthropic", "Anthropic Claude pricing page changed (4 lines added, 2 removed)",
                      "https://claude.com/pricing", "claude.com", now,
                      "ADDED: Haiku 5.5 $0.10 / $0.50 per M tokens", category="pricing",
                      importance=4, official=True, via="page-diff"))
    return items


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="AI Competitor Watch Agent")
    ap.add_argument("companies", nargs="*", help="5-10 names, optionally 'Name|alias'")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-importance", type=int, default=2)
    ap.add_argument("--per-company", type=int, default=25,
                    help="max news stories per company sent to the LLM")
    ap.add_argument("--sources", default=os.path.join(HERE, "sources.json"))
    ap.add_argument("--state", default=os.path.join(HERE, "state"))
    ap.add_argument("--out", default=os.path.join(HERE, "reports", dt.date.today().isoformat()))
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--check-sources", action="store_true")
    ap.add_argument("--open", action="store_true", help="open the HTML brief when done")
    ap.add_argument("--print", action="store_true", help="also print the Markdown brief")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cfg = load_sources(a.sources)
    cfg_names = [k for k in cfg if not k.startswith("_")]
    targets = a.companies or cfg_names or ["OpenAI", "Anthropic", "Mistral AI", "Cohere"]
    if a.check_sources:
        check_sources(targets, cfg)
        return
    if not a.demo and not (1 <= len(targets) <= 10):
        ap.error("provide between 1 and 10 companies (5-10 recommended)")
    names_by_company = build_names(targets, cfg)

    if a.demo:
        raw, days = demo_items(), 30
    else:
        days = a.days
        raw = collect_news(targets, days) + collect_feeds(targets, cfg) + \
            watch_pages(targets, cfg, a.state)
    stats = {"raw": len(raw)}
    print(f"Collected {len(raw)} raw items")
    filtered = noise_filter(raw, names_by_company, days)
    stats["after_noise"] = len(filtered)
    unique = dedupe(filtered, names_by_company)
    stats["unique"] = len(unique)

    use_llm = not a.no_llm
    if use_llm and llm_available():          # cap news per company to limit LLM cost
        for it in unique:
            heuristic_classify(it)
        capped = []
        for t in targets:
            nme = parse_target(t)[0]
            mine = [i for i in unique if i.company == nme]
            capped += [i for i in mine if i.official] + sorted(
                (i for i in mine if not i.official),
                key=lambda i: (-i.importance, -i.published.timestamp()))[:a.per_company]
        print(f"Cap: {len(unique)} -> {len(capped)} stories sent to LLM")
        unique = capped
    relevant = classify(unique, use_llm=use_llm)
    final = [i for i in relevant if i.importance >= a.min_importance]
    print(f"Final: {len(final)} items after relevance + importance filter")

    summary = make_summary(final, use_llm=use_llm)
    os.makedirs(a.out, exist_ok=True)
    md = render_markdown(final, summary, targets, days, stats)
    paths = {"brief.md": md, "brief.html": render_html(final, summary, targets, days, stats),
             "updates.json": json.dumps([i.to_dict() for i in rank(final)], indent=2)}
    for fname, content in paths.items():
        with open(os.path.join(a.out, fname), "w", encoding="utf-8") as f:
            f.write(content)

    print(f"\nDone. Report folder: {a.out}")
    print("Top signals:")
    for n, t in enumerate(summary["top_signals"], 1):
        print(f"  {n}. [{t['item'].company}] {t['item'].summary[:110]}")
    if a.print:
        print("\n" + md)
    if os.environ.get("SLACK_WEBHOOK_URL"):
        try:
            post_slack(os.environ["SLACK_WEBHOOK_URL"], summary)
            print("Posted to Slack")
        except Exception as e:
            print(f"  ! Slack post failed: {e}", file=sys.stderr)
    if a.open:
        import webbrowser
        from pathlib import Path
        webbrowser.open(Path(os.path.join(a.out, "brief.html")).resolve().as_uri())


if __name__ == "__main__":
    main()
