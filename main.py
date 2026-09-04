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
ARCHIVE_PATH = STATE_DIR / "archive.json"
PAGE_SIZE = 20

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

# Strani kontekst proveravamo u naslovu + sažetku, ne samo u naslovu.
# To hvata npr. naslov "Uber otpušta..." čiji opis kaže "američka kompanija".
FOREIGN_CONTEXT_RX = re.compile(
    r"\b("
    r"grck\w*|rumun\w*|madjar\w*|nemack\w*|kanad\w*|kong\w*|"
    r"francusk\w*|italij\w*|spanij\w*|americ\w*|kinesk\w*|"
    r"palestin\w*|izrael\w*|ukrajin\w*|rusij\w*|hrvatsk\w*|"
    r"bosn\w*|hercegovin\w*|bugarsk\w*|austrij\w*|svajcarsk\w*|"
    r"britan\w*|englesk\w*|poljsk\w*|sloven\w*|svedsk\w*|"
    r"dansk\w*|norvesk\w*|finsk\w*|holand\w*|belgij\w*|"
    r"cesk\w*|slovack\w*|portugal\w*|irsk\w*|"
    r"nujork\w*|san francisk\w*|barselon\w*|stokolm\w*|"
    r"lufthanz\w*|vestdzet\w*|evzoni\w*|paks\w*"
    r")\b|crna gora|severna makedon|sjedinjen\w+\s+americ\w+\s+drzav",
    re.I,
)

# Strani marker se ne odbacuje ako tekst jasno smešta radničko pitanje u Srbiju.
# Ne koristimo samo reč "Srbija": "Er Srbija ... štrajk u Barseloni" nije domaći štrajk.
SERBIA_CONTEXT_RX = re.compile(
    r"\b("
    r"beograd\w*|novi sad\w*|nis\w*|kragujev\w*|kraljev\w*|"
    r"cacak\w*|uzic\w*|zrenjan\w*|subotic\w*|leskov\w*|vranj\w*|"
    r"prokuplj\w*|novi pazar\w*|smederev\w*|pancev\w*|krusev\w*|"
    r"sabac\w*|valjev\w*|zajecar\w*|jagodin\w*|sombor\w*|"
    r"kikind\w*|vrsac\w*|loznic\w*|pozarev\w*|batajnic\w*|"
    r"cajetin\w*|pozeg\w*|arilj\w*|bajin\w*|priboj\w*|prijepolj\w*|"
    r"sjenic\w*|vojvodin\w*|sumadij\w*|zlatibor\w*|kolubar\w*"
    r")\b|"
    r"\bu srbij\w*|\biz srbij\w*|\bvlada srbije\b|"
    r"\b(radni[ck]|zaposlen|sindikat|poslodav)\w*.{0,35}\bsrbij\w*|"
    r"\bsrpsk\w*.{0,25}\b(radni[ck]|zaposlen|fabrik|preduzec|sindikat)\w*",
    re.I,
)

# Rubrike čiji URL već eksplicitno kaže da članak nije domaća vest.
FOREIGN_URL_PARTS = (
    "/vesti/eu/",
    "/vesti/svet/",
    "/vesti/region/",
)

# RSS/Atom kategorije daju dodatni geografski signal.
# Npr. Biznis.rs članak može imati kategoriju "Svet" iako URL/naslov
# ne sadrže ime strane države.
FOREIGN_SOURCE_CATEGORY_RX = re.compile(
    r"\b(svet|eu|inostranstv\w*|globaln\w*)\b",
    re.I,
)

SERBIA_SOURCE_CATEGORY_RX = re.compile(
    r"\b(srbij\w*|domac\w*)\b",
    re.I,
)

# Strani događaj često nema ime države u naslovu:
# "širom sveta", "u Evropi", "na nivou cele grupe" itd.
WORLD_SCOPE_RX = re.compile(
    r"\b("
    r"sirom\s+sveta|"
    r"u\s+evropi|"
    r"evropsk\w*.{0,24}fabrik\w*|"
    r"fabrik\w*.{0,24}evrop\w*|"
    r"na\s+nivou\s+(cele\s+)?grupe|"
    r"na\s+globalnom\s+nivou|"
    r"globaln\w*.{0,30}(radn\w+\s+mest|zaposlen|otpust)|"
    r"van\s+srbije"
    r")\b",
    re.I,
)

# Berzanska/investiciona "zarada" nije zarada radnika.
# Ne odbacujemo samo reč "akcije", jer ona može značiti i radničke akcije.
MARKET_PROMO_RX = re.compile(
    r"\bakcij\w*.{0,60}\b(skoc|porast|pao|pala|pad|cen|vredn|berz|invest)\w*|"
    r"\b(skoc|porast|pao|pala|pad|cen|vredn|berz|invest)\w*.{0,60}\bakcij\w*|"
    r"\bkript\w*.{0,45}\b(invest|trgov|zarad)\w*",
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
    r"\b(gradilist|fabrik|pogon|rudnik|rudarsk|kop|hala|masin|dizalic|"
    r"utovar|putar)\w*|"
    r"\b(radno mesto|tokom rada|na poslu|na radu)\b|"
    r"\bmontazn\w*.{0,12}\bplac\w*|"
    r"\brekonstrukcij\w*.{0,24}\b(put|ulic)\w*|"
    r"\bradov\w*.{0,20}\b(put|ulic|gradilist|kop)\w*|"
    r"\bna\s+koje[mj]\s+su\s+radil\w*|"
    r"\bradil\w*.{0,24}\b(put|ulic|gradilist|kop)\w*",
    re.I,
)

WORKER_HARM_RX = re.compile(
    r"\b(radni[ck]|zaposlen)\w*.{0,80}\b(povred|pogin|strad|premin)\w*|"
    r"\b(povred|pogin|strad|premin)\w*.{0,80}\b(radni[ck]|zaposlen)\w*",
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
    source_categories: list[str] | None = None

    def __post_init__(self):
        if self.categories is None:
            self.categories = []
        if self.reasons is None:
            self.reasons = []
        if self.source_categories is None:
            self.source_categories = []


def hard_reject(item: Item) -> str:
    title = norm_text(item.title)
    text = norm_text(f"{item.title} {item.summary}")
    url = norm_text(item.url)
    source_categories = norm_text(" ".join(item.source_categories or []))

    has_serbia_context = bool(
        SERBIA_CONTEXT_RX.search(text)
        or SERBIA_SOURCE_CATEGORY_RX.search(source_categories)
    )

    if HUNGER_STRIKE_RX.search(text):
        if not (HUNGER_WORKER_RX.search(text) and HUNGER_LABOR_ISSUE_RX.search(text)):
            return "štrajk glađu bez jasnog radničkog konteksta"

    if any(part in url for part in NON_LABOR_URL_PARTS):
        return "sportska/zabavna/svetska rubrika"

    if ENTERTAINMENT_SPORT_RX.search(text):
        return "sport/estrada"

    if any(part in url for part in FOREIGN_URL_PARTS):
        return "strana/regionalna rubrika"

    if MARKET_PROMO_RX.search(title):
        return "berza/investiciona promocija"

    if (
        FOREIGN_SOURCE_CATEGORY_RX.search(source_categories)
        and not has_serbia_context
    ):
        return "strana kategorija izvora"

    if (
        FOREIGN_CONTEXT_RX.search(text)
        or WORLD_SCOPE_RX.search(text)
    ) and not has_serbia_context:
        return "radničko pitanje van Srbije"

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

    # 6) Otkazi.
    # "Otkazivanje motora/leta/skupa" više nije dovoljno samo zato što se
    # u opisu negde pojavljuje "kompanija".
    strong_layoff = re.search(
        r"\botpust\w*|\btehnolosk\w+\s+visak\b|"
        r"\bukidanj\w*.{0,30}\bradn\w+\s+mest\w*|"
        r"\bgasen\w*.{0,30}\bradn\w+\s+mest\w*|"
        r"\bosta\w*.{0,22}\bbez\s+posla\b",
        text,
    )
    employment_otkaz = re.search(
        r"\b(radni[ck]|zaposlen|sindikat)\w*.{0,60}\botkaz\w*|"
        r"\botkaz\w*.{0,60}\b(radni[ck]|zaposlen|sindikat)\w*|"
        r"\b(dobio|dobila|dobili|dobile|urucen|urucena|uruceni)\w*.{0,25}\botkaz\w*|"
        r"\botkaz\w*.{0,35}\b(ugovor\w*\s+o\s+radu|radni odnos)\b",
        text,
    )
    if strong_layoff and LABOR_ACTOR_RX.search(text):
        add("Otkazi / radna mesta", "otpuštanje/gubitak posla + radni odnos")
    elif employment_otkaz:
        add("Otkazi / radna mesta", "otkaz + zaposleni/radnik/sindikat")

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
        tried_415_fallback = False

        for attempt in range(self.retries):
            try:
                r = self.session.get(
                    url, timeout=self.timeout, allow_redirects=True, **kwargs
                )
                if r.status_code == 200:
                    return r

                # Neki WAF/proxy serveri povremeno odbijaju naš široki Accept
                # profil sa HTTP 415 čak i za običan GET. U tom slučaju samo
                # jednom ponovi isti zahtev sa neutralnim Accept: */*.
                if r.status_code == 415 and not tried_415_fallback:
                    tried_415_fallback = True
                    retry_kwargs = dict(kwargs)
                    retry_headers = dict(retry_kwargs.pop("headers", {}) or {})
                    retry_headers["Accept"] = "*/*"

                    retry = self.session.get(
                        url,
                        timeout=self.timeout,
                        allow_redirects=True,
                        headers=retry_headers,
                        **retry_kwargs,
                    )
                    if retry.status_code == 200:
                        return retry

                    last = RuntimeError(
                        f"HTTP {retry.status_code} za {url} "
                        f"posle 415 retry-ja sa Accept: */*"
                    )

                    # Ako i neutralni zahtev vraća 415, nema smisla ponavljati
                    # isti obrazac još nekoliko puta; prepusti izvor fallback-u.
                    if retry.status_code == 415:
                        break

                    r = retry
                else:
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
    p = urlparse(page_url)
    path = p.path.rstrip("/") + "/feed/"
    return urlunparse((p.scheme, p.netloc, path, "", "", ""))


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

        source_categories = []
        for tag in entry.get("tags", []) or []:
            if isinstance(tag, dict):
                term = tag.get("term", "")
            else:
                term = getattr(tag, "term", "")
            term = re.sub(r"\s+", " ", str(term or "")).strip()
            if term:
                source_categories.append(term)

        category = re.sub(
            r"\s+", " ", str(entry.get("category", "") or "")
        ).strip()
        if category:
            source_categories.append(category)

        source_categories = list(dict.fromkeys(source_categories))

        out.append(
            Item(
                title=title,
                url=link,
                source=source["name"],
                source_kind="rss",
                summary=summary[:900],
                date=date[:100],
                source_categories=source_categories,
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


def _find_article_body_json(obj) -> str:
    if isinstance(obj, dict):
        body = obj.get("articleBody")
        if isinstance(body, str) and len(body.strip()) > 80:
            return body.strip()
        for value in obj.values():
            found = _find_article_body_json(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_article_body_json(value)
            if found:
                return found
    return ""


def extract_article_body(page_text: str) -> str:
    """Izvuci tekst članka iz JSON-LD articleBody ili <article>/<main>."""
    soup = BeautifulSoup(page_text, "html.parser")

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        body = _find_article_body_json(data)
        if body:
            return re.sub(r"\s+", " ", body).strip()[:5000]

    node = soup.find("article") or soup.find("main")
    if node:
        for bad in node.find_all(["script", "style", "nav", "aside", "footer"]):
            bad.decompose()
        return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()[:5000]

    return ""


def enrich_work_accident_candidates(
    fetcher: Fetcher, items: list[Item], max_fetches: int = 12
) -> None:
    """
    Ako naslov/RSS opis kaže da je radnik poginuo ili povređen, ali ne kaže
    dovoljno da znamo da li se to desilo tokom rada, pročitaj samo taj članak.
    """
    fetched = 0

    for item in items:
        if fetched >= max_fetches:
            break

        current = norm_text(f"{item.title} {item.summary}")
        if not WORKER_HARM_RX.search(current):
            continue
        if WORKSITE_RX.search(current) or VIOLENCE_RX.search(current):
            continue

        try:
            r = fetcher.get(item.url)
            body = extract_article_body(r.text)
            if not body:
                continue
            item.summary = re.sub(
                r"\s+", " ", f"{item.summary} {body}"
            ).strip()[:5000]
            fetched += 1
            time.sleep(0.15)
        except Exception:
            # Ovo je pomoćni korak; neuspeh ne ruši ceo collector.
            continue


def collect_source(
    fetcher: Fetcher, source: dict, max_items: int
) -> tuple[list[Item], dict]:
    status = {
        "source": source["name"],
        "url": source["url"],
        "method": "",
        "feed_url": "",
        "fallback_url": "",
        "feed_error": "",
        "candidates": 0,
        "error": "",
    }

    errors = []

    # Ako znamo tačan feed, probaj njega pre landing stranice.
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
            errors.append(f'feed: {status["feed_error"]}')
        except Exception as e:
            status["feed_error"] = str(e)
            errors.append(f"feed: {e}")

    # Zatim kategorijska/listing stranica.
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
        if items or not source.get("fallback_url"):
            status["method"] = "html"
            status["candidates"] = len(items)
            return items, status

        errors.append("stranica je vraćena, ali parser nije našao kandidate")

    except Exception as e:
        errors.append(f"stranica: {e}")

    # Neki mali sajtovi uporno blokiraju GitHub datacenter IP (403).
    # Za njih original ostaje prvi izbor, a Naslovi.net je rezervni izvor.
    fallback_url = source.get("fallback_url", "").strip()
    if fallback_url:
        try:
            page = fetcher.get(fallback_url)
            mirror_source = {**source, "url": page.url}
            soup = BeautifulSoup(page.text, "html.parser")
            fallback_max = int(source.get("fallback_max_items", 12))
            items = parse_listing_html(
                soup, mirror_source, min(max_items, fallback_max)
            )

            for item in items:
                item.source_kind = "mirror"

            if items:
                status["method"] = "mirror"
                status["fallback_url"] = page.url
                status["candidates"] = len(items)
                status["error"] = ""
                return items, status

            errors.append("fallback je dostupan, ali nema parsabilnih kandidata")
        except Exception as e:
            errors.append(f"fallback: {e}")

    status["method"] = "error"
    status["error"] = "; ".join(errors) or "izvor nije dostupan"
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


def load_archive() -> list[dict]:
    """Učitaj trajnu arhivu svih ranije prihvaćenih članaka."""
    if not ARCHIVE_PATH.exists():
        return []

    try:
        data = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict) and x.get("url")]
    except Exception:
        pass

    return []


def archive_record_timestamp(record: dict) -> float:
    """Datum članka ima prednost; first_seen je rezervni datum za sortiranje."""
    temp = Item(
        title=record.get("title", ""),
        url=record.get("url", ""),
        source=record.get("source", ""),
        source_kind=record.get("source_kind", ""),
        date=record.get("date", ""),
    )
    ts = item_timestamp(temp)
    if ts:
        return ts

    first_seen = (record.get("first_seen") or "").strip()
    if first_seen:
        try:
            return datetime.fromisoformat(
                first_seen.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            pass

    return 0.0


def merge_archive(existing: list[dict], current_items: list[Item]) -> list[dict]:
    """
    Dodaj nove članke u arhivu i osveži metapodatke za već poznate URL-ove.
    first_seen ostaje datum kada je Hronika prvi put videla članak.
    """
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    by_url: dict[str, dict] = {}

    for record in existing:
        url = clean_url(record.get("url", ""))
        if not url:
            continue
        copy = dict(record)
        copy["url"] = url
        copy.setdefault("first_seen", now)
        by_url[url] = copy

    for item in current_items:
        url = clean_url(item.url)
        if not url:
            continue

        previous = by_url.get(url, {})
        by_url[url] = {
            "title": item.title,
            "url": url,
            "source": item.source,
            "source_kind": item.source_kind,
            "summary": item.summary,
            "date": item.date,
            "categories": list(item.categories or []),
            "reasons": list(item.reasons or []),
            "query": item.query,
            "source_categories": list(item.source_categories or []),
            "first_seen": previous.get("first_seen") or now,
            "last_seen": now,
        }

    archive = list(by_url.values())
    archive.sort(key=archive_record_timestamp, reverse=True)
    return archive


def save_archive(records: list[dict]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    ARCHIVE_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
            if s.get("fallback_url"):
                detail += f' · fallback: {s["fallback_url"]}'
            if s.get("feed_error") and s.get("method") not in ("rss", "mirror"):
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


def write_paginated_html(
    archive: list[dict],
    current_items: list[Item],
    statuses: list[dict],
    hidden_urls: set[str],
) -> Path:
    """
    Prikaži trajnu arhivu po 20 članaka po strani.
    Navigacija koristi ?page=2, ?page=3... i radi na GitHub Pages bez servera.
    """
    OUT_DIR.mkdir(exist_ok=True)

    visible_archive = [
        record
        for record in archive
        if clean_url(record.get("url", "")) not in hidden_urls
    ]

    generated = datetime.now().astimezone().strftime("%d.%m.%Y. %H:%M")
    ok = sum(1 for s in statuses if not s.get("error"))
    failed = sum(1 for s in statuses if s.get("error"))
    new_urls = sorted({clean_url(x.url) for x in current_items if x.is_new})

    archive_json = json.dumps(
        visible_archive, ensure_ascii=False
    ).replace("</", "<\\/")
    new_urls_json = json.dumps(
        new_urls, ensure_ascii=False
    ).replace("</", "<\\/")

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
            if s.get("fallback_url"):
                detail += f' · fallback: {s["fallback_url"]}'
            if s.get("feed_error") and s.get("method") not in ("rss", "mirror"):
                detail += f' · RSS nije uspeo: {s["feed_error"]}'
        status_rows.append(
            f"<tr><td>{esc(s['source'])}</td><td>{state}</td>"
            f"<td>{esc(str(detail))}</td></tr>"
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
  .stats {{
    padding: 12px 14px; background: white; border: 1px solid #ddd; margin: 22px 0;
  }}
  .item {{
    background: white; border: 1px solid #ddd; padding: 16px 18px; margin: 12px 0;
  }}
  .item h2 {{ font-size: 1.12rem; margin: 7px 0; line-height: 1.3; }}
  a {{ color: #111; }}
  .meta, .why {{ color: #666; font-size: .84rem; }}
  .summary {{ margin: 8px 0; color: #333; }}
  .tag {{
    display: inline-block; border: 1px solid #bbb; padding: 2px 7px;
    margin: 4px 5px 4px 0; font-size: .78rem;
  }}
  .new {{
    font-size: .72rem; font-weight: 700; border: 1px solid #111;
    padding: 2px 5px; margin-right: 7px;
  }}
  .pager {{
    display: flex; flex-wrap: wrap; align-items: center; justify-content: center;
    gap: 6px; margin: 20px 0;
  }}
  .pager a, .pager span {{
    display: inline-block; min-width: 28px; padding: 5px 8px;
    border: 1px solid #bbb; background: white; text-align: center;
    text-decoration: none;
  }}
  .pager .current {{
    background: #171717; color: white; border-color: #171717;
  }}
  .pager .disabled {{ color: #aaa; }}
  .pager .dots {{
    border-color: transparent; background: transparent; min-width: auto;
  }}
  details {{ margin-top: 28px; }}
  table {{
    border-collapse: collapse; width: 100%; font-size: .82rem; background: white;
  }}
  td, th {{
    border: 1px solid #ddd; padding: 6px; text-align: left; vertical-align: top;
  }}
</style>
</head>
<body>
<h1>Radnička hronika</h1>
<p class="lead">
  Vesti o radnim pravima, zaradama, štrajkovima, uslovima rada,
  otkazima i bezbednosti radnika u Srbiji.
</p>

<div class="stats">
  Poslednje osvežavanje: <strong>{esc(generated)}</strong><br>
  Ukupno u arhivi: <strong>{len(visible_archive)}</strong>
  · novih u ovom prolazu: <strong>{len(new_urls)}</strong><br>
  Izvora/upita bez greške: <strong>{ok}</strong> · sa greškom: <strong>{failed}</strong>
</div>

<div id="pager-top" class="pager" aria-label="Navigacija kroz arhivu"></div>
<div id="items"></div>
<div id="pager-bottom" class="pager" aria-label="Navigacija kroz arhivu"></div>

<details id="source-status">
  <summary>Tehnički status izvora</summary>
  <table>
    <thead><tr><th>Izvor</th><th>Status</th><th>Detalj</th></tr></thead>
    <tbody>{''.join(status_rows)}</tbody>
  </table>
</details>

<script id="archive-data" type="application/json">{archive_json}</script>
<script id="new-url-data" type="application/json">{new_urls_json}</script>

<script>
(function () {{
  const PAGE_SIZE = {PAGE_SIZE};
  const archive = JSON.parse(
    document.getElementById("archive-data").textContent || "[]"
  );
  const newUrls = new Set(JSON.parse(
    document.getElementById("new-url-data").textContent || "[]"
  ));

  function escapeHtml(value) {{
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }}

  function pageUrl(n) {{
    const u = new URL(window.location.href);
    if (n <= 1) {{
      u.searchParams.delete("page");
    }} else {{
      u.searchParams.set("page", String(n));
    }}
    return u.pathname + u.search + u.hash;
  }}

  const params = new URLSearchParams(window.location.search);
  let page = parseInt(params.get("page") || "1", 10);
  const totalPages = Math.max(1, Math.ceil(archive.length / PAGE_SIZE));

  if (!Number.isFinite(page) || page < 1) page = 1;
  if (page > totalPages) page = totalPages;

  function card(item) {{
    const badges = (item.categories || [])
      .map(c => `<span class="tag">${{escapeHtml(c)}}</span>`)
      .join("");

    const reasons = (item.reasons || []).join(", ");
    const date = item.date ? ` · ${{escapeHtml(item.date)}}` : "";
    const summary = item.summary
      ? `<p class="summary">${{escapeHtml(String(item.summary).slice(0, 420))}}</p>`
      : "";
    const novo = newUrls.has(item.url)
      ? '<span class="new">NOVO</span>'
      : "";

    return `
      <article class="item">
        <div class="meta">${{novo}}<strong>${{escapeHtml(item.source || "")}}</strong>${{date}}</div>
        <h2><a href="${{escapeHtml(item.url || "")}}" target="_blank"
          rel="noopener noreferrer">${{escapeHtml(item.title || "")}}</a></h2>
        ${{summary}}
        <div class="tags">${{badges}}</div>
        <div class="why">razlog: ${{escapeHtml(reasons)}} · preko:
          ${{escapeHtml(item.source_kind || "")}}</div>
      </article>
    `;
  }}

  function pager() {{
    if (totalPages <= 1) return "";

    const parts = [];

    if (page > 1) {{
      parts.push(`<a href="${{pageUrl(page - 1)}}">← Novije</a>`);
    }} else {{
      parts.push('<span class="disabled">← Novije</span>');
    }}

    const wanted = new Set([1, totalPages]);
    for (let n = page - 2; n <= page + 2; n++) {{
      if (n >= 1 && n <= totalPages) wanted.add(n);
    }}

    const nums = Array.from(wanted).sort((a, b) => a - b);
    let prev = 0;

    for (const n of nums) {{
      if (prev && n - prev > 1) {{
        parts.push('<span class="dots">…</span>');
      }}

      if (n === page) {{
        parts.push(
          `<span class="current" aria-current="page">${{n}}</span>`
        );
      }} else {{
        parts.push(`<a href="${{pageUrl(n)}}">${{n}}</a>`);
      }}
      prev = n;
    }}

    if (page < totalPages) {{
      parts.push(`<a href="${{pageUrl(page + 1)}}">Starije →</a>`);
    }} else {{
      parts.push('<span class="disabled">Starije →</span>');
    }}

    return parts.join("");
  }}

  const start = (page - 1) * PAGE_SIZE;
  const pageItems = archive.slice(start, start + PAGE_SIZE);
  const itemsNode = document.getElementById("items");

  if (pageItems.length) {{
    itemsNode.innerHTML = pageItems.map(card).join("");
  }} else {{
    itemsNode.innerHTML =
      "<p><strong>Arhiva je trenutno prazna.</strong></p>";
  }}

  const pagerHtml = pager();
  document.getElementById("pager-top").innerHTML = pagerHtml;
  document.getElementById("pager-bottom").innerHTML = pagerHtml;

  // Današnji status izvora prikazuj samo uz prvu, najnoviju stranu.
  if (page !== 1) {{
    document.getElementById("source-status").hidden = true;
  }}

  function sendHeight() {{
    const h = Math.max(
      document.body ? document.body.scrollHeight : 0,
      document.documentElement ? document.documentElement.scrollHeight : 0
    );
    if (window.parent && window.parent !== window) {{
      window.parent.postMessage(
        {{ type: "radnicka-hronika:height", height: h }},
        "*"
      );
    }}
  }}

  window.addEventListener("load", sendHeight);
  window.addEventListener("resize", sendHeight);
  setTimeout(sendHeight, 100);
  setTimeout(sendHeight, 500);

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
    ("EPS: Radnik poginuo u Kolubari - pukla sajla dizalice prilikom utovara", "", True),
    (
        "Dva radnika poginula kada je kamion naleteo na njih na putu Kljajićevo - Sivac",
        "",
        True,
        "Radnici su se kretali putem na kojem su radili.",
    ),
    (
        "Uber otpušta 3.300 zaposlenih",
        "",
        False,
        "Američka kompanija Uber smanjuje broj zaposlenih u Njujorku i San Francisku.",
    ),
    (
        "Volkswagenov plan za otpuštanje 100.000 zaposlenih pod velikom osudom",
        "/vesti/eu/volkswagen-otpustanja/",
        False,
    ),
    ("Najduži štrajk u Švedskoj: Tesla i IF Metall", "", False),
    (
        "Er Srbija putnicima za Barselonu preporučuje samo ručni prtljag jer je na tamošnjem aerodromu štrajk",
        "",
        False,
    ),
    (
        "SAD pokreću istragu o milion vozila Dženeral motorsa zbog otkazivanja motora",
        "",
        False,
        "Američke vlasti istražuju kvarove i otkazivanje motora na vozilima.",
    ),
    (
        "Akcije Moderne skočile 177 odsto u jednom danu: Još nije kasno da i Vi zaradite",
        "",
        False,
        "Cena akcije raste, investitori prate berzu.",
    ),
    (
        "Nadzorni odbor Volkswagena odobrio plan za ukidanje još 50.000 radnih mesta",
        "",
        False,
        "",
        ["Svet"],
    ),
    (
        "Volkswagen planira ukidanje još 50.000 radnih mesta",
        "",
        False,
        "Smanjenje broja zaposlenih sprovodi se širom sveta i na nivou cele grupe.",
    ),
    (
        "Nemačka kompanija planira ukidanje 300 radnih mesta u Kragujevcu",
        "",
        True,
        "",
        ["Svet"],
    ),

]


def run_self_test() -> int:
    failures = 0
    for case in SELF_TESTS:
        if len(case) == 3:
            title, url, expected = case
            summary = ""
            source_categories = []
        elif len(case) == 4:
            title, url, expected, summary = case
            source_categories = []
        else:
            title, url, expected, summary, source_categories = case

        item = Item(
            title=title,
            url=f"https://primer.rs{url}",
            source="test",
            source_kind="rss",
            summary=summary,
            source_categories=source_categories,
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
    enrich_work_accident_candidates(fetcher, raw_items)
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

    archive = merge_archive(load_archive(), relevant)
    save_archive(archive)

    write_debug(raw_items, relevant, rejected, statuses)
    json_path = write_json(relevant)
    html_path = write_paginated_html(
        archive,
        relevant,
        statuses,
        hidden_urls,
    )
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
