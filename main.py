#!/usr/bin/env python3
"""
Radnička hronika v2
-------------------
Cilj: VISOKA PRECIZNOST, ne maksimalan broj pogodaka.

Tok:
  1) jedan stroži GDELT upit vezan za Srbiju
  2) RSS/Atom za eksplicitno praćene domaće izvore
  3) HTML kategorijska stranica ako sekcijski RSS ne postoji
  4) deterministički filter za radni odnos / radna prava
  5) odbacivanje očiglednog inostranstva, sporta/estrade, štrajka glađu itd.
  6) deduplikacija po URL-u
  7) JSON + HTML pregled

Nema AI/ML klasifikacije.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_UA = "RadnickaHronika/0.2 (+contact: kostacrn@gmail.com)"

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "out"
STATE_DIR = BASE_DIR / "state"
DEBUG_DIR = BASE_DIR / "debug"
SOURCES_PATH = BASE_DIR / "sources.json"
HIDDEN_URLS_PATH = BASE_DIR / "hidden_urls.txt"

# GDELT sourcecountry znači "medij iz Srbije", a ne "događaj u Srbiji".
# GDELT DOC API traži najmanje 5 sekundi između zahteva.
# Zato GDELT ima sopstveni pacing/retry i ne koristi generički Fetcher.get().
GDELT_QUERY = (
    '(strike OR layoffs OR "job cuts" OR "unpaid wages" OR "workers rights" '
    'OR "working conditions" OR "workplace injury" OR "work accident" '
    'OR "worker injured" OR "worker killed" OR mobbing) '
    "Serbia sourcecountry:serbia"
)

GDELT_FALLBACK_QUERY = (
    '(strike OR layoffs OR "unpaid wages" OR "workplace injury" OR "worker killed") '
    "Serbia sourcecountry:serbia"
)


# ---------------------------------------------------------------------------
# Normalizacija
# ---------------------------------------------------------------------------

CYR_TO_LAT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "ђ": "dj",
        "е": "e",
        "ж": "z",
        "з": "z",
        "и": "i",
        "ј": "j",
        "к": "k",
        "л": "l",
        "љ": "lj",
        "м": "m",
        "н": "n",
        "њ": "nj",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "ћ": "c",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "c",
        "ч": "c",
        "џ": "dz",
        "ш": "s",
        "А": "a",
        "Б": "b",
        "В": "v",
        "Г": "g",
        "Д": "d",
        "Ђ": "dj",
        "Е": "e",
        "Ж": "z",
        "З": "z",
        "И": "i",
        "Ј": "j",
        "К": "k",
        "Л": "l",
        "Љ": "lj",
        "М": "m",
        "Н": "n",
        "Њ": "nj",
        "О": "o",
        "П": "p",
        "Р": "r",
        "С": "s",
        "Т": "t",
        "Ћ": "c",
        "У": "u",
        "Ф": "f",
        "Х": "h",
        "Ц": "c",
        "Ч": "c",
        "Џ": "dz",
        "Ш": "s",
    }
)


def norm_text(s: str) -> str:
    s = (s or "").translate(CYR_TO_LAT).lower().replace("đ", "dj")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Precizni filter
# ---------------------------------------------------------------------------

# Ovi URL putevi gotovo uvek znače da je kategorijski RSS skliznuo na
# sadržaj koji nema veze sa našom namenom.
NON_LABOR_URL_PARTS = (
    "/sport/",
    "/zabava/",
    "/jetset/",
    "/vip/",
    "/rijaliti/",
    "/kultura/",
    "/svet/",
    "/planeta/",
)

# Ako je naslov očigledno o inostranstvu, odbaci ga. Lista nije zamišljena
# kao kompletna geografija sveta — samo sprečava čest šum.
FOREIGN_TITLE_RX = re.compile(
    r"\b("
    r"grck\w*|rumun\w*|madjar\w*|nemack\w*|kanad\w*|kong\w*|"
    r"francusk\w*|italij\w*|spanij\w*|americ\w*|kinesk\w*|"
    r"palestin\w*|izrael\w*|ukrajin\w*|rusij\w*|hrvatsk\w*|"
    r"bugarsk\w*|austrij\w*|svajcarsk\w*|britan\w*|englesk\w*|"
    r"poljsk\w*|sloven\w*|lufthanz\w*|vestdzet\w*|audi\w*|"
    r"evzoni\w*|paks\w*"
    r")\b|crna gora|severna makedon",
    re.I,
)

# Ako naslov istovremeno eksplicitno govori o Srbiji, ne odbacuj samo zato
# što pominje stranu državu/kompaniju.
SERBIA_TITLE_RX = re.compile(
    r"\b("
    r"srbij\w*|beograd\w*|novi sad\w*|kragujev\w*|kraljev\w*|"
    r"cacak\w*|uzic\w*|zrenjan\w*|subotic\w*|leskov\w*|vranj\w*|"
    r"prokuplj\w*|novi pazar\w*|smederev\w*|pancev\w*|krusev\w*|"
    r"sabac\w*|valjev\w*|zajecar\w*|jagodin\w*|sombor\w*|"
    r"kikind\w*|vrsac\w*|loznic\w*|pozarev\w*|batajnic\w*"
    r")\b",
    re.I,
)

ENTERTAINMENT_SPORT_RX = re.compile(
    r"\b(teniser|fudbal|kosark|igrac|trener|koncert|reper|rijaliti|estrad)\w*",
    re.I,
)

HUNGER_STRIKE_RX = re.compile(
    r"\bstrajk\w*.{0,14}\bgladj\w*|\bgladj\w*.{0,14}\bstrajk\w*",
    re.I,
)

# Štrajk glađu NIJE automatski odbačen: prolazi ako je jasno da ga vode
# radnici/zaposleni/sindikat i da je povod konkretno radno pitanje.
HUNGER_WORKER_RX = re.compile(
    r"\b(radni[ck]|zaposlen|sindikat|prosvet|nastavnik|ucitelj|lekar|vozac|rudar)\w*",
    re.I,
)

HUNGER_LABOR_ISSUE_RX = re.compile(
    r"\b(plat|zarad|akontacij|otkaz|otpust|poslodav|radn\w+\s+prav|"
    r"uslov\w+\s+rada|ugovor\w*\s+o\s+radu|radn\w+\s+mest|"
    r"prekovremen|neprijavljen|minimalac|minimaln\w+\s+zarad)\w*",
    re.I,
)

LABOR_ACTOR_RX = re.compile(
    r"\b("
    r"radni[ck]|zaposlen|poslodav|sindikat|strajkack|"
    r"prosvet|nastavnik|ucitelj|lekar|vozac|rudar|"
    r"fabrik|preduzec|kompanij|firma|vodovod|radni odnos|ugovor o radu"
    r")\w*",
    re.I,
)

WORKSITE_RX = re.compile(
    r"\b(gradilist|fabrik|pogon|rudnik|hala|masin|radno mesto|tokom rada|na poslu)\w*",
    re.I,
)

VIOLENCE_RX = re.compile(
    r"\b(napad|napadac|pistolj|sekir|ranjen|tuca|pretuk)\w*",
    re.I,
)


@dataclass
class Item:
    title: str
    url: str
    source: str
    source_kind: str
    summary: str = ""
    date: str = ""
    categories: list[str] | None = None
    reasons: list[str] | None = None
    is_new: bool = True
    query: str = ""

    def __post_init__(self):
        if self.categories is None:
            self.categories = []
        if self.reasons is None:
            self.reasons = []


def hard_reject(item: Item) -> str:
    title = norm_text(item.title)
    text = norm_text(f"{item.title} {item.summary}")
    url = norm_text(item.url)

    if HUNGER_STRIKE_RX.search(text):
        if not (HUNGER_WORKER_RX.search(text) and HUNGER_LABOR_ISSUE_RX.search(text)):
            return "štrajk glađu bez jasnog radničkog konteksta"

    if any(part in url for part in NON_LABOR_URL_PARTS):
        return "sportska/zabavna/svetska rubrika"

    if ENTERTAINMENT_SPORT_RX.search(text):
        return "sport/estrada"

    if FOREIGN_TITLE_RX.search(title) and not SERBIA_TITLE_RX.search(title):
        return "očigledno inostranstvo"

    return ""


def classify_item(item: Item) -> tuple[list[str], list[str], str]:
    """
    Vrati: (kategorije, razlozi, reject_reason).

    Princip v2:
    - "radnik" sam po sebi NIJE dovoljan;
    - "otkaz" sam po sebi NIJE dovoljan;
    - "stradao radnik" NIJE povreda na radu bez konteksta rada;
    - štrajk glađu nije radnički štrajk;
    - inostrani štrajk nije događaj za Radničku hroniku.
    """
    rejected = hard_reject(item)
    if rejected:
        return [], [], rejected

    text = norm_text(f"{item.title} {item.summary}")
    categories: list[str] = []
    reasons: list[str] = []

    def add(category: str, reason: str):
        if category not in categories:
            categories.append(category)
            reasons.append(reason)

    # 1) Mobing i eksplicitna radna prava
    if re.search(r"\bmobing\w*", text):
        add("Radna prava / mobing", "mobing")
    elif re.search(r"\bzlostavlj\w*.{0,25}\bna radu\b", text):
        add("Radna prava / mobing", "zlostavljanje na radu")
    elif re.search(r"\bradnick\w+\s+prav\w*", text):
        add("Radna prava / mobing", "radnička prava")
    elif re.search(r"\b(povred|krsen)\w*.{0,30}\bradn\w+\s+prav\w*", text):
        add("Radna prava / mobing", "povreda radnih prava")
    elif re.search(r"\bpravo\w*.{0,20}\bsindikaln\w+\s+organiz\w*", text):
        add("Radna prava / mobing", "sindikalno organizovanje")

    # 2) Neprijavljen rad
    if re.search(r"\bneprijavljen\w*.{0,18}\b(radni[ck]|zaposlen)\w*", text):
        add("Neprijavljen rad", "neprijavljeni radnici")
    elif re.search(r"\brad\w*\s+na\s+crno\b", text):
        add("Neprijavljen rad", "rad na crno")

    # 3) Uslovi i status rada — samo karakteristične fraze
    if re.search(r"\b(rad|ugovor)\w*.{0,18}\bna odredjeno\b", text):
        add("Uslovi rada", "rad na određeno")
    if re.search(r"\bprekovremen\w*", text):
        add("Uslovi rada", "prekovremeni rad")
    if re.search(r"\bbez\s+zastit\w*.{0,15}\boprem\w*", text):
        add("Uslovi rada", "bez zaštitne opreme")
    if re.search(r"\b(nebezbed|nehuman|los|tesk)\w*.{0,30}\buslov\w+\s+rada\b", text):
        add("Uslovi rada", "loši/nebezbedni uslovi rada")

    # 4) Neisplaćene / umanjene plate, zarade, akontacije
    wage_nonpayment_patterns = (
        (
            r"\b(neisplac|neisplat)\w*.{0,35}\b(zarad|plat|akontacij)\w*",
            "neisplaćena zarada/akontacija",
        ),
        (
            r"\b(zarad|plat|akontacij)\w*.{0,35}\b(neisplac|neisplat)\w*",
            "neisplaćena zarada/akontacija",
        ),
        (
            r"\bnije\s+isplac\w*.{0,30}\b(zarad|plat|akontacij)\w*",
            "nije isplaćena zarada/akontacija",
        ),
        (
            r"\bnisu\s+isplac\w*.{0,30}\b(zarad|plat|akontacij)\w*",
            "nisu isplaćene zarade",
        ),
        (
            r"\bbez\s+(plate|plata|zarade|zarada|akontacije|akontacija)\b",
            "bez plate/akontacije",
        ),
        (
            r"\bnisu\s+(primili|dobili)\b.{0,35}\b(zarad|plat|akontacij)\w*",
            "nisu primili zaradu/akontaciju",
        ),
        (
            r"\b(umanjen|umanjiv)\w*.{0,20}\b(zarad|plat|akontacij)\w*",
            "umanjena zarada/akontacija",
        ),
        (r"\bkasn\w*.{0,22}\b(zarad|plat)\w*", "kašnjenje zarade"),
    )
    for pattern, reason in wage_nonpayment_patterns:
        if re.search(pattern, text):
            add("Neisplaćene / umanjene zarade", reason)
            break

    if re.search(r"\bminimalac\b|\bminimaln\w+\s+zarad\w*", text) and re.search(
        r"\b(sindikat|zaposlen|radni[ck]|sss|ugs|pregovor)\w*", text
    ):
        add("Zarade / minimalac", "minimalna zarada + radnički/sindikalni kontekst")

    # 5) Štrajk — mora biti radnička akcija, a ne samo reč "štrajk"
    if re.search(r"\bstrajk\w*|\bobustav\w*.{0,18}\brad\w*", text):
        if LABOR_ACTOR_RX.search(text):
            add("Štrajk / radnička akcija", "štrajk/obustava rada + radnički kontekst")
    elif re.search(
        r"\b(radni[ck]|zaposlen)\w*.{0,35}\bprotest\w*|"
        r"\bprotest\w*.{0,65}\b(radni[ck]|zaposlen)\w*",
        text,
    ):
        add("Radnički protest", "radnici/zaposleni protestuju")

    # 6) Otkazi — "otpustio oca/trenera" više ne prolazi
    layoff = re.search(
        r"\botpust\w*|\botkaz\w*|\btehnolosk\w+\s+visak\b|"
        r"\bukidanj\w*.{0,25}\bradn\w+\s+mest\w*|"
        r"\bgasen\w*.{0,25}\bradn\w+\s+mest\w*",
        text,
    )
    if layoff and LABOR_ACTOR_RX.search(text):
        add("Otkazi / radna mesta", "otkaz/otpuštanje + radni odnos")

    # 7) Povrede/smrti: eksplicitno "na radu" je dovoljno.
    if re.search(r"\bpovred\w*.{0,25}\bna radu\b|\bnesrec\w+\s+na\s+radu\b", text):
        add("Povreda na radu", "povreda/nesreća na radu")
    elif re.search(r"\b(pogin|strad|premin)\w*.{0,25}\b(na radu|radnom mestu)\b", text):
        add("Smrt na radu", "smrt na radu/radnom mestu")
    else:
        # Slabiji oblik "radnik povređen/poginuo" zahteva fizički kontekst
        # radnog mesta i odbacuje očigledne napade/tuče.
        injury = re.search(
            r"\b(radni[ck]|zaposlen)\w*.{0,65}\b(povred|pogin|strad|premin)\w*|"
            r"\b(povred|pogin|strad|premin)\w*.{0,65}\b(radni[ck]|zaposlen)\w*",
            text,
        )
        if injury and WORKSITE_RX.search(text) and not VIOLENCE_RX.search(text):
            if re.search(r"\b(pogin|strad|premin)\w*", text):
                add("Smrt na radu", "radnik stradao + kontekst radnog mesta")
            else:
                add("Povreda na radu", "radnik povređen + kontekst radnog mesta")

    # 8) Sindikat nije dovoljan sam za sebe; mora postojati konkretno radno pitanje.
    if re.search(r"\bsindikat\w*", text) and re.search(
        r"\b(neisplac|isplat|zarad|plat|minimal|otkaz|otpust|strajk|"
        r"uslov|prav|tuzb|spor|akontacij|protest|kolektivn\w+\s+ugovor)\w*",
        text,
    ):
        add("Sindikalno pitanje", "sindikat + konkretno pitanje rada")

    if not categories:
        return [], [], "nema dovoljno jakog radno-pravnog signala"

    return categories, reasons, ""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Fetcher:
    def __init__(self, user_agent: str, retries: int = 3, timeout: int = 25):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "sr,en;q=0.8",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
            }
        )
        self.retries = retries
        self.timeout = timeout

    def get(self, url: str, **kwargs) -> requests.Response:
        last = None
        for attempt in range(self.retries):
            try:
                r = self.session.get(
                    url, timeout=self.timeout, allow_redirects=True, **kwargs
                )
                if r.status_code == 200:
                    return r
                last = RuntimeError(f"HTTP {r.status_code} za {url}")
                # 429: poštuj Retry-After ako ga server pošalje.
                if r.status_code == 429:
                    try:
                        wait = float(r.headers.get("Retry-After", "4"))
                    except Exception:
                        wait = 4
                    time.sleep(max(wait, 2.0 * (attempt + 1)))
                    continue
            except Exception as e:
                last = e
            time.sleep(1.2 * (2**attempt))
        raise RuntimeError(f"Neuspešan GET {url}: {last}")


# ---------------------------------------------------------------------------
# URL / source helpers
# ---------------------------------------------------------------------------


def clean_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
        return urlunparse(
            (p.scheme or "https", p.netloc.lower(), p.path, p.params, p.query, "")
        )
    except Exception:
        return url.strip()


def same_site(url: str, base_url: str) -> bool:
    try:
        a = urlparse(url).netloc.lower().removeprefix("www.")
        b = urlparse(base_url).netloc.lower().removeprefix("www.")
        return bool(a and b and (a == b or a.endswith("." + b) or b.endswith("." + a)))
    except Exception:
        return False


BAD_LINK_TEXT = {
    "naslovna",
    "početna",
    "pocetna",
    "vesti",
    "društvo",
    "drustvo",
    "ekonomija",
    "politika",
    "biznis",
    "opširnije",
    "opsirnije",
    "više",
    "vise",
    "sledeća strana",
    "sledeca strana",
    "kontakt",
    "o nama",
    "marketing",
    "search",
    "pretraga",
}

BAD_HREF_PARTS = (
    "/tag/",
    "/author/",
    "/category/",
    "/kategorija/",
    "/wp-login",
    "/wp-admin",
    "/feed/",
    "/kontakt",
    "/contact",
    "/o-nama",
    "/about",
    "/politika-privatnosti",
    "/privacy",
    "#",
    "javascript:",
    "mailto:",
)


def plausible_article_link(title: str, url: str, source_url: str) -> bool:
    t = norm_text(title)
    if len(t) < 18 or len(t.split()) < 3:
        return False
    if t in {norm_text(x) for x in BAD_LINK_TEXT}:
        return False
    if any(part in url.lower() for part in BAD_HREF_PARTS):
        return False
    if not same_site(url, source_url):
        return False
    return True


# ---------------------------------------------------------------------------
# RSS / HTML
# ---------------------------------------------------------------------------


def section_feed_guess(page_url: str) -> str:
    return page_url.rstrip("/") + "/feed/"


def root_feed_guess(page_url: str) -> str:
    p = urlparse(page_url)
    return f"{p.scheme}://{p.netloc}/feed/"


def discovered_feeds(soup: BeautifulSoup, page_url: str, scope: str) -> list[str]:
    out = []
    page_path = urlparse(page_url).path.rstrip("/")
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        typ = (link.get("type") or "").lower()
        href = link.get("href")
        if not (href and "alternate" in rel and ("rss" in typ or "atom" in typ)):
            continue
        feed = urljoin(page_url, href)
        if scope == "site":
            out.append(feed)
            continue

        # Kod sekcije NE prihvataj automatski globalni /feed/.
        feed_path = urlparse(feed).path.rstrip("/")
        if page_path and feed_path.startswith(page_path):
            out.append(feed)
    return out


def feed_candidates(soup: BeautifulSoup, page_url: str, scope: str) -> list[str]:
    candidates = []
    if scope == "section":
        candidates.append(section_feed_guess(page_url))
    else:
        candidates.append(root_feed_guess(page_url))
    candidates.extend(discovered_feeds(soup, page_url, scope))
    return list(dict.fromkeys(candidates))


def parse_feed_bytes(content: bytes, source: dict, max_items: int) -> list[Item]:
    parsed = feedparser.parse(content)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        return []

    out = []
    for entry in parsed.entries[:max_items]:
        title = BeautifulSoup(entry.get("title", ""), "html.parser").get_text(
            " ", strip=True
        )
        link = clean_url(entry.get("link", ""))
        if not title or not link or not same_site(link, source["url"]):
            continue
        summary_html = entry.get("summary", "") or entry.get("description", "")
        summary = BeautifulSoup(summary_html, "html.parser").get_text(" ", strip=True)
        date = entry.get("published", "") or entry.get("updated", "")
        out.append(
            Item(
                title=title,
                url=link,
                source=source["name"],
                source_kind="rss",
                summary=summary[:900],
                date=date[:100],
            )
        )
    return out


def parse_listing_html(soup: BeautifulSoup, source: dict, max_items: int) -> list[Item]:
    page_url = source["url"]
    found: list[Item] = []
    used = set()

    def add(title: str, href: str, context: str = "", date: str = ""):
        if len(found) >= max_items:
            return
        url = clean_url(urljoin(page_url, href))
        title = re.sub(r"\s+", " ", title or "").strip()
        if not plausible_article_link(title, url, page_url):
            return
        if url in used:
            return
        used.add(url)

        context = re.sub(r"\s+", " ", context or "").strip()
        if context.startswith(title):
            context = context[len(title) :].strip(" -–—|:")

        found.append(
            Item(
                title=title,
                url=url,
                source=source["name"],
                source_kind="html",
                summary=context[:900],
                date=(date or "")[:100],
            )
        )

    for node in soup.find_all("article"):
        heading = node.find(["h1", "h2", "h3", "h4"])
        a = heading.find("a", href=True) if heading else None
        if a:
            time_node = node.find("time")
            date = ""
            if time_node:
                date = time_node.get("datetime") or time_node.get_text(" ", strip=True)
            add(
                a.get_text(" ", strip=True),
                a["href"],
                node.get_text(" ", strip=True),
                date,
            )

    if len(found) < 5:
        for heading in soup.find_all(["h2", "h3", "h4"]):
            a = heading.find("a", href=True)
            if a:
                parent = heading.parent
                context = parent.get_text(" ", strip=True) if parent else ""
                time_node = parent.find("time") if parent else None
                date = (
                    (time_node.get("datetime") or time_node.get_text(" ", strip=True))
                    if time_node
                    else ""
                )
                add(a.get_text(" ", strip=True), a["href"], context, date)

    if len(found) < 5:
        for a in soup.find_all("a", href=True):
            title = a.get_text(" ", strip=True)
            parent = a.parent
            context = parent.get_text(" ", strip=True) if parent else ""
            add(title, a["href"], context)

    return found[:max_items]


def collect_source(
    fetcher: Fetcher, source: dict, max_items: int
) -> tuple[list[Item], dict]:
    status = {
        "source": source["name"],
        "url": source["url"],
        "method": "",
        "feed_url": "",
        "feed_error": "",
        "candidates": 0,
        "error": "",
    }

    # V4: ako u sources.json već znamo tačan feed, probaj NJEGA PRE landing
    # stranice. Ovo je bitno za sajtove koji GitHub Actions runneru vraćaju
    # 403 na kategorijskoj stranici, iako im RSS normalno postoji.
    explicit_feed = source.get("feed_url", "").strip()
    if explicit_feed:
        try:
            r = fetcher.get(explicit_feed)
            items = parse_feed_bytes(r.content, source, max_items)
            if items:
                status["method"] = "rss"
                status["feed_url"] = r.url
                status["candidates"] = len(items)
                return items, status
            status["feed_error"] = "feed je vraćen, ali nije dao parsabilne stavke"
        except Exception as e:
            status["feed_error"] = str(e)

    # Ako direktni feed ne uspe, probaj samu kategorijsku stranicu i
    # autodiscovery/generički HTML parser.
    try:
        page = fetcher.get(source["url"])
        final_source = {**source, "url": page.url}
        soup = BeautifulSoup(page.text, "html.parser")
        scope = source.get("scope", "section")

        for feed_url in feed_candidates(soup, page.url, scope)[:4]:
            if explicit_feed and clean_url(feed_url) == clean_url(explicit_feed):
                continue
            try:
                r = fetcher.get(feed_url)
                items = parse_feed_bytes(r.content, final_source, max_items)
                if items:
                    status["method"] = "rss"
                    status["feed_url"] = r.url
                    status["candidates"] = len(items)
                    return items, status
            except Exception:
                pass

        items = parse_listing_html(soup, final_source, max_items)
        status["method"] = "html"
        status["candidates"] = len(items)
        return items, status

    except Exception as e:
        status["method"] = "error"
        if status["feed_error"]:
            status["error"] = f'feed: {status["feed_error"]}; stranica: {e}'
        else:
            status["error"] = str(e)
        return [], status


# ---------------------------------------------------------------------------
# GDELT
# ---------------------------------------------------------------------------


def fetch_gdelt(
    fetcher: Fetcher, timespan: str, maxrecords: int
) -> tuple[list[Item], dict]:
    """
    GDELT DOC API traži najmanje 5 sekundi između zahteva.

    Zato ovde ne koristimo Fetcher.get(), čiji je generički retry namenjen
    običnim sajtovima i može biti brži od GDELT limita.

    - između dva GDELT zahteva držimo najmanje 6 sekundi;
    - na HTTP 429 čekamo 7 sekundi i isti zahtev probamo još jednom;
    - ako i drugi pokušaj dobije 429, odustajemo od GDELT-a za taj run;
    - fallback koristimo samo kod drugih grešaka / nevalidnog JSON odgovora.
    """
    status = {
        "source": "GDELT:Srbija-radnici",
        "url": GDELT_ENDPOINT,
        "method": "gdelt",
        "feed_url": "",
        "query_variant": "",
        "candidates": 0,
        "error": "",
    }

    min_interval = 6.0
    retry_after_429 = 7.0
    last_request_at: float | None = None

    def paced_request(params: dict) -> requests.Response:
        nonlocal last_request_at

        if last_request_at is not None:
            elapsed = time.monotonic() - last_request_at
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)

        r = fetcher.session.get(
            GDELT_ENDPOINT,
            params=params,
            timeout=fetcher.timeout,
            allow_redirects=True,
        )
        last_request_at = time.monotonic()
        return r

    def parse_response(r: requests.Response, label: str) -> list[Item]:
        body = r.text or ""
        ctype = (r.headers.get("Content-Type") or "").lower()

        if r.status_code != 200:
            head = re.sub(r"\s+", " ", body[:260]).strip() or "<prazan odgovor>"
            raise RuntimeError(f"HTTP {r.status_code}; početak={head!r}")

        try:
            data = r.json()
        except Exception:
            DEBUG_DIR.mkdir(exist_ok=True)
            debug_text = (
                f"URL: {r.url}\n"
                f"HTTP: {r.status_code}\n"
                f"Content-Type: {r.headers.get('Content-Type', '')}\n\n"
                f"{body[:100000]}"
            )
            (DEBUG_DIR / f"gdelt_bad_response_{label}.txt").write_text(
                debug_text, encoding="utf-8", errors="ignore"
            )
            head = re.sub(r"\s+", " ", body[:260]).strip() or "<prazan odgovor>"
            raise RuntimeError(
                f"nije JSON (HTTP {r.status_code}, "
                f"Content-Type={ctype or '?'}, početak={head!r})"
            )

        articles = data.get("articles") or []
        out: list[Item] = []

        for a in articles:
            title = a.get("title", "") or ""
            url = clean_url(a.get("url", "") or "")
            if not title or not url:
                continue

            out.append(
                Item(
                    title=title,
                    url=url,
                    source=a.get("domain", "") or "GDELT",
                    source_kind="gdelt",
                    summary="",
                    date=a.get("seendate", "") or "",
                    query=label,
                )
            )

        status["query_variant"] = label
        status["candidates"] = len(articles)
        return out

    variants = [
        ("primary", GDELT_QUERY, min(maxrecords, 75)),
        ("fallback", GDELT_FALLBACK_QUERY, min(maxrecords, 50)),
    ]
    errors = []

    for label, query, limit in variants:
        params = {
            "query": query,
            "mode": "artlist",
            "format": "JSON",
            "timespan": timespan,
            "sort": "datedesc",
            "maxrecords": limit,
        }

        try:
            r = paced_request(params)

            if r.status_code == 429:
                first_message = re.sub(r"\s+", " ", (r.text or "")[:260]).strip()

                time.sleep(retry_after_429)
                r = paced_request(params)

                if r.status_code == 429:
                    second_message = re.sub(
                        r"\s+", " ", (r.text or "")[:260]
                    ).strip()
                    status["error"] = (
                        f"{label}: HTTP 429 i posle ponovnog pokušaja; "
                        f"GDELT poruka="
                        f"{(second_message or first_message or '<prazno>')!r}"
                    )
                    return [], status

            items = parse_response(r, label)
            return items, status

        except Exception as e:
            errors.append(f"{label}: {e}")
            # Sledeći variant, ako ga bude, paced_request će sačekati
            # da prođe najmanje 6 sekundi od prethodnog GDELT zahteva.
            continue

    status["error"] = " | ".join(errors)
    return [], status


# ---------------------------------------------------------------------------
# Dedupe / state / output
# ---------------------------------------------------------------------------


def dedupe(items: Iterable[Item]) -> list[Item]:
    seen = set()
    out = []
    for item in items:
        key = clean_url(item.url)
        if not key or key in seen:
            continue
        seen.add(key)
        item.url = key
        out.append(item)
    return out


def filter_relevant(items: Iterable[Item]) -> tuple[list[Item], list[dict]]:
    accepted = []
    rejected = []

    for item in items:
        cats, reasons, reject_reason = classify_item(item)
        if cats:
            item.categories = cats
            item.reasons = reasons
            accepted.append(item)
        else:
            rejected.append(
                {
                    "title": item.title,
                    "url": item.url,
                    "source": item.source,
                    "source_kind": item.source_kind,
                    "reject_reason": reject_reason,
                }
            )

    return accepted, rejected


def load_hidden_urls() -> set[str]:
    """
    URL-ovi koje administrator ručno želi da sakrije.
    Jedan URL po redu u hidden_urls.txt; prazni redovi i # komentari se ignorišu.
    """
    if not HIDDEN_URLS_PATH.exists():
        return set()

    hidden = set()
    for line in HIDDEN_URLS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        hidden.add(clean_url(line))
    return hidden


def load_seen() -> set[str]:
    path = STATE_DIR / "seen_urls.json"
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(urls: Iterable[str]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / "seen_urls.json").write_text(
        json.dumps(sorted(set(urls)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def item_timestamp(item: Item) -> float:
    value = (item.date or "").strip()
    if not value:
        return 0.0

    try:
        if re.fullmatch(r"\d{8}T\d{6}Z", value):
            return (
                datetime.strptime(value, "%Y%m%dT%H%M%SZ")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
    except Exception:
        pass

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        pass

    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass

    m = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", value)
    if m:
        try:
            return datetime(
                int(m.group(3)),
                int(m.group(2)),
                int(m.group(1)),
                tzinfo=timezone.utc,
            ).timestamp()
        except Exception:
            pass

    return 0.0


def filter_by_age(
    items: list[Item], max_age_days: int
) -> tuple[list[Item], list[dict]]:
    """
    Odbaci lokalne RSS/HTML članke sa pouzdano parsabilnim datumom starijim od max_age_days.
    Ako datum nedostaje ili ga ne umemo parsirati, članak ostaje da ga relevance filter proceni.
    GDELT već ima sopstveni timespan i ovde ga ne diramo.
    """
    if max_age_days <= 0:
        return items, []

    cutoff = (
        datetime.now(timezone.utc).timestamp()
        - timedelta(days=max_age_days).total_seconds()
    )
    kept = []
    rejected = []

    for item in items:
        if item.source_kind == "gdelt":
            kept.append(item)
            continue

        ts = item_timestamp(item)
        if ts and ts < cutoff:
            rejected.append(
                {
                    "title": item.title,
                    "url": item.url,
                    "source": item.source,
                    "source_kind": item.source_kind,
                    "reject_reason": f"starije od {max_age_days} dana",
                }
            )
        else:
            kept.append(item)

    return kept, rejected


def write_debug(
    raw_items: list[Item],
    accepted: list[Item],
    rejected: list[dict],
    statuses: list[dict],
) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)

    with (DEBUG_DIR / "raw_candidates.jsonl").open("w", encoding="utf-8") as f:
        for item in raw_items:
            f.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")

    with (DEBUG_DIR / "accepted.jsonl").open("w", encoding="utf-8") as f:
        for item in accepted:
            f.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")

    with (DEBUG_DIR / "rejected.jsonl").open("w", encoding="utf-8") as f:
        for item in rejected:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    (DEBUG_DIR / "source_status.json").write_text(
        json.dumps(statuses, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_json(items: list[Item]) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / "latest.json"
    payload = {
        "generated_at": datetime.now(timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds"),
        "count": len(items),
        "items": [asdict(x) for x in items],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def write_html(items: list[Item], statuses: list[dict]) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    generated = datetime.now().astimezone().strftime("%d.%m.%Y. %H:%M")
    ok = sum(1 for s in statuses if not s.get("error"))
    failed = sum(1 for s in statuses if s.get("error"))
    new_count = sum(1 for x in items if x.is_new)

    cards = []
    for item in items:
        badges = "".join(f'<span class="tag">{esc(c)}</span>' for c in item.categories)
        reasons = ", ".join(item.reasons)
        new_badge = '<span class="new">NOVO</span>' if item.is_new else ""
        summary = (
            f'<p class="summary">{esc(item.summary[:420])}</p>' if item.summary else ""
        )
        date = f" · {esc(item.date)}" if item.date else ""

        cards.append(f"""
        <article class="item">
          <div class="meta">{new_badge}<strong>{esc(item.source)}</strong>{date}</div>
          <h2><a href="{esc(item.url)}" target="_blank" rel="noopener noreferrer">{esc(item.title)}</a></h2>
          {summary}
          <div class="tags">{badges}</div>
          <div class="why">razlog: {esc(reasons)} · preko: {esc(item.source_kind)}</div>
        </article>
        """)

    status_rows = []
    for s in statuses:
        state = "GREŠKA" if s.get("error") else "OK"
        if s.get("error"):
            detail = s["error"]
        else:
            detail = f'{s.get("method","")} · {s.get("candidates",0)} kandidata'
            if s.get("feed_url"):
                detail += f' · {s["feed_url"]}'
            if s.get("query_variant"):
                detail += f' · upit: {s["query_variant"]}'
            if s.get("feed_error") and s.get("method") != "rss":
                detail += f' · RSS nije uspeo: {s["feed_error"]}'
        status_rows.append(
            f"<tr><td>{esc(s['source'])}</td><td>{state}</td><td>{esc(str(detail))}</td></tr>"
        )

    doc = f"""<!doctype html>
<html lang="sr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Radnička hronika</title>
<style>
  body {{
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 920px; margin: 0 auto; padding: 28px 18px 60px;
    color: #171717; background: #fafafa; line-height: 1.45;
  }}
  h1 {{ margin-bottom: 4px; }}
  .lead {{ color: #555; margin-top: 0; }}
  .stats {{ padding: 12px 14px; background: white; border: 1px solid #ddd; margin: 22px 0; }}
  .item {{ background: white; border: 1px solid #ddd; padding: 16px 18px; margin: 12px 0; }}
  .item h2 {{ font-size: 1.12rem; margin: 7px 0; line-height: 1.3; }}
  a {{ color: #111; }}
  .meta, .why {{ color: #666; font-size: .84rem; }}
  .summary {{ margin: 8px 0; color: #333; }}
  .tag {{ display: inline-block; border: 1px solid #bbb; padding: 2px 7px; margin: 4px 5px 4px 0; font-size: .78rem; }}
  .new {{ font-size: .72rem; font-weight: 700; border: 1px solid #111; padding: 2px 5px; margin-right: 7px; }}
  details {{ margin-top: 28px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .82rem; background: white; }}
  td, th {{ border: 1px solid #ddd; padding: 6px; text-align: left; vertical-align: top; }}
</style>
</head>
<body>
<h1>Radnička hronika</h1>
<p class="lead">Vesti o radnim pravima, zaradama, štrajkovima, uslovima rada, otkazima i bezbednosti radnika u Srbiji. Oznaka NOVO znači da link nije viđen u prethodnom automatskom prolazu.</p>

<div class="stats">
  Poslednje osvežavanje: <strong>{esc(generated)}</strong><br>
  Pogodaka u ovom prolazu: <strong>{len(items)}</strong> · novih: <strong>{new_count}</strong><br>
  Izvora/upita bez greške: <strong>{ok}</strong> · sa greškom: <strong>{failed}</strong>
</div>

{''.join(cards) if cards else '<p><strong>Nema novih relevantnih pogodaka u ovom prolazu.</strong></p>'}

<details>
  <summary>Tehnički status izvora</summary>
  <table>
    <thead><tr><th>Izvor</th><th>Status</th><th>Detalj</th></tr></thead>
    <tbody>{''.join(status_rows)}</tbody>
  </table>
</details>

<script>
(function () {{
  function sendHeight() {{
    var h = Math.max(
      document.body ? document.body.scrollHeight : 0,
      document.documentElement ? document.documentElement.scrollHeight : 0
    );
    if (window.parent && window.parent !== window) {{
      window.parent.postMessage({{ type: "radnicka-hronika:height", height: h }}, "*");
    }}
  }}

  window.addEventListener("load", sendHeight);
  window.addEventListener("resize", sendHeight);
  setTimeout(sendHeight, 250);
  setTimeout(sendHeight, 1200);

  if (window.ResizeObserver && document.documentElement) {{
    new ResizeObserver(sendHeight).observe(document.documentElement);
  }}
}})();
</script>
</body>
</html>
"""
    path = OUT_DIR / "report.html"
    path.write_text(doc, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

SELF_TESTS = [
    ("Radnici Jure stupili u štrajk zbog uslova rada", "", True),
    ("Radnik pao sa skele na gradilištu i teško povređen", "", True),
    ("Povreda na radu u fabrici u Kragujevcu", "", True),
    ("Otpušteno 120 zaposlenih u fabrici", "", True),
    ("Zaposleni tri meseca bez plate", "", True),
    ("Radnice prijavile mobing na poslu", "", True),
    ("Srpski poslodavci drže svakog sedmog radnika na određeno vreme", "", True),
    ("Četiri neprijavljena radnika pronašla inspekcija", "", True),
    ("Sindikat: zaposlenima nije isplaćena akontacija za jul", "", True),
    ("Radnici fabrike počeli štrajk glađu zbog neisplaćenih plata", "", True),
    ("Zaposleni stupili u štrajk glađu zbog otkaza i uslova rada", "", True),
    ("Studentski štrajk nastavljen i danas", "", False),
    ("Kristina odustala od štrajka glađu", "/zabava/", False),
    ("Štrajk grčkih carinika napravio gužve na granici", "", False),
    ("Rumunija: zdravstveni radnici stupili u štrajk", "", False),
    ("Direktor napadnut, a radnik umalo stradao od napadača", "", False),
    ("Cicipas žali što ranije nije otpustio oca", "/sport/", False),
    ("Utakmica otkazana zbog kiše", "/sport/", False),
    ("Cena nafte porasla dva odsto", "", False),
]


def run_self_test() -> int:
    failures = 0
    for title, url, expected in SELF_TESTS:
        item = Item(
            title=title, url=f"https://primer.rs{url}", source="test", source_kind="rss"
        )
        got = bool(classify_item(item)[0])
        mark = "OK" if got == expected else "FAIL"
        print(f"[{mark}] expected={expected:<5} got={got:<5} | {title}")
        failures += got != expected
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Radnička hronika — collector v2")
    ap.add_argument("--timespan", default="14d", help="GDELT period, npr. 6d ili 14d")
    ap.add_argument(
        "--maxrecords",
        type=int,
        default=75,
        help="GDELT max; V4 interno ograničava primarni upit na 75",
    )
    ap.add_argument(
        "--max-per-source", type=int, default=40, help="Kandidata po lokalnom izvoru"
    )
    ap.add_argument(
        "--local-days",
        type=int,
        default=30,
        help="Odbaci lokalne članke sa poznatim datumom starijim od N dana; 0=gasi filter",
    )
    ap.add_argument(
        "--delay", type=float, default=0.45, help="Pauza između lokalnih izvora"
    )
    ap.add_argument("--no-gdelt", action="store_true", help="Preskoči GDELT")
    ap.add_argument("--only", default="", help="Samo izvori čije ime sadrži ovaj tekst")
    ap.add_argument(
        "--fresh", action="store_true", help="Označi sve trenutne pogotke kao NOVO"
    )
    ap.add_argument(
        "--self-test", action="store_true", help="Test filtera bez interneta"
    )
    ap.add_argument(
        "--user-agent",
        default=os.environ.get("RADNICKA_HRONIKA_UA", DEFAULT_UA),
        help="User-Agent sa kontaktom",
    )
    args = ap.parse_args()

    if args.self_test:
        return run_self_test()

    OUT_DIR.mkdir(exist_ok=True)
    STATE_DIR.mkdir(exist_ok=True)
    DEBUG_DIR.mkdir(exist_ok=True)

    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    if args.only:
        needle = norm_text(args.only)
        sources = [s for s in sources if needle in norm_text(s.get("name", ""))]

    fetcher = Fetcher(args.user_agent)
    raw_items: list[Item] = []
    statuses: list[dict] = []

    if not args.no_gdelt:
        print(f"[GDELT] period={args.timespan}", flush=True)
        gdelt_items, gdelt_status = fetch_gdelt(fetcher, args.timespan, args.maxrecords)
        raw_items.extend(gdelt_items)
        statuses.append(gdelt_status)
        print(f"[GDELT] kandidata: {len(gdelt_items)}", flush=True)

    for idx, source in enumerate(sources, start=1):
        if source.get("enabled", True) is False:
            continue
        print(f"[{idx}/{len(sources)}] {source['name']} ...", flush=True)
        found, status = collect_source(fetcher, source, args.max_per_source)
        raw_items.extend(found)
        statuses.append(status)

        if status["error"]:
            print(f"    GREŠKA: {status['error']}", flush=True)
        else:
            via = status["method"]
            if status.get("feed_url"):
                via += f" ({status['feed_url']})"
            print(f"    {via}: {len(found)} kandidata", flush=True)

        time.sleep(max(0, args.delay))

    raw_items = dedupe(raw_items)
    age_filtered_items, age_rejected = filter_by_age(raw_items, args.local_days)
    relevant, rejected = filter_relevant(age_filtered_items)
    rejected.extend(age_rejected)
    relevant = dedupe(relevant)

    hidden_urls = load_hidden_urls()
    if hidden_urls:
        hidden_rejected = []
        kept = []
        for item in relevant:
            if clean_url(item.url) in hidden_urls:
                hidden_rejected.append(
                    {
                        "title": item.title,
                        "url": item.url,
                        "source": item.source,
                        "source_kind": item.source_kind,
                        "reject_reason": "ručno sakriven URL",
                    }
                )
            else:
                kept.append(item)
        relevant = kept
        rejected.extend(hidden_rejected)

    seen_before = set() if args.fresh else load_seen()
    for item in relevant:
        item.is_new = item.url not in seen_before

    relevant.sort(
        key=lambda x: (1 if x.is_new else 0, item_timestamp(x), x.source.lower()),
        reverse=True,
    )

    write_debug(raw_items, relevant, rejected, statuses)
    json_path = write_json(relevant)
    html_path = write_html(relevant, statuses)
    save_seen(seen_before | {x.url for x in relevant})

    print()
    print("=== GOTOVO ===")
    print(f"Raw kandidata:  {len(raw_items)}")
    print(f"Prihvaćeno:     {len(relevant)}")
    print(f"Odbačeno:       {len(rejected)}")
    print(f"HTML:           {html_path}")
    print(f"JSON:           {json_path}")
    print(f"Debug rejected: {DEBUG_DIR / 'rejected.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
