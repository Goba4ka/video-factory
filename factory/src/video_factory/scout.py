from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .errors import ValidationError


NASA_RSS_URL = "https://www.nasa.gov/news-release/feed/"
WIKIMEDIA_FEATURED_URL = (
    "https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{year}/{month}/{day}"
)
NOAA_OCEANFACTS_URL = "https://oceanservice.noaa.gov/rss/oceanfacts.xml"
NOAA_NOSNEWS_URL = "https://oceanservice.noaa.gov/rss/nosnews.xml"
NOAA_NOSMEDIA_URL = "https://oceanservice.noaa.gov/newsroom/nosmedia.xml"
USGS_SIGNIFICANT_MONTH_URL = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson"
)
ESA_SPACE_SCIENCE_URL = "https://www.esa.int/rssfeed/Our_Activities/Space_Science"
LOC_LATEST_NEWS_URL = "https://www.loc.gov/rss/pao/news.xml"
LOC_FOLKLIFE_URL = "https://blogs.loc.gov/folklife/feed/"
USER_AGENT = "video-factory-scout/0.6 (+local editorial research)"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

Transport = Callable[[str, float], str]


@dataclass(frozen=True)
class DiscoveryItem:
    provider: str
    publisher: str
    title: str
    summary: str
    url: str
    published_at: str | None
    primary: bool


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    publisher: str
    endpoint: str
    parser: Callable[[str], list[DiscoveryItem]]


@dataclass(frozen=True)
class CachedResponse:
    source_id: str
    endpoint: str
    fetched_at: str
    body: str


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(_TAG_RE.sub(" ", value))
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    shortened = text[: max(1, limit - 1)].rstrip(" ,;:-")
    return f"{shortened}…"


def _safe_title(value: Any) -> str:
    title = _clean_text(value, limit=120)
    return title if len(title) >= 5 else f"Topic: {title or 'untitled item'}"


def _safe_summary(value: Any, title: str) -> str:
    summary = _clean_text(value, limit=300)
    if len(summary) >= 10:
        return summary
    return _clean_text(f"Official source item about {title}.", limit=300)


def _valid_http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value.startswith(("https://", "http://")) else None


def _parse_feed_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _iso(parsed)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return _iso(parsed)
        except (TypeError, ValueError, OverflowError):
            return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _child_text(node: ET.Element, names: Sequence[str]) -> str | None:
    wanted = {name.casefold() for name in names}
    for child in node:
        if _local_name(child.tag) in wanted:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return None


def _child_link(node: ET.Element) -> str | None:
    fallback: str | None = None
    for child in node:
        if _local_name(child.tag) not in {"link", "guid", "id"}:
            continue
        href = _valid_http_url(child.attrib.get("href"))
        text = _valid_http_url("".join(child.itertext()).strip())
        candidate = href or text
        if candidate is None:
            continue
        rel = child.attrib.get("rel", "alternate").casefold()
        if rel == "alternate":
            return candidate
        fallback = fallback or candidate
    return fallback


def parse_generic_feed(
    body: str,
    *,
    provider: str,
    publisher: str,
    primary: bool,
) -> list[DiscoveryItem]:
    if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
        raise ValueError("XML document type declarations are not accepted")
    try:
        root = ET.fromstring(body)
        nodes = [
            node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}
        ]
    except ET.ParseError as document_error:
        # Some long-lived government feeds contain one malformed historical entry.
        # Recover only independently well-formed RSS items; never repair or invent text.
        nodes = []
        for match in re.finditer(r"<item\b[^>]*>.*?</item\s*>", body, re.I | re.S):
            try:
                nodes.append(ET.fromstring(match.group(0)))
            except ET.ParseError:
                continue
        if not nodes:
            raise document_error
    items: list[DiscoveryItem] = []
    seen_urls: set[str] = set()
    for node in nodes:
        url = _child_link(node)
        if url is None or url in seen_urls:
            continue
        seen_urls.add(url)
        title = _safe_title(_child_text(node, ("title",)))
        summary = _safe_summary(
            _child_text(node, ("description", "summary", "content", "encoded")),
            title,
        )
        items.append(
            DiscoveryItem(
                provider=provider,
                publisher=publisher,
                title=title,
                summary=summary,
                url=url,
                published_at=_parse_feed_date(
                    _child_text(node, ("pubDate", "published", "updated", "date"))
                ),
                primary=primary,
            )
        )
    if not items:
        raise ValueError(f"{provider} feed contained no usable items")
    return items


def generic_feed_parser(
    provider: str,
    publisher: str,
    *,
    primary: bool = True,
) -> Callable[[str], list[DiscoveryItem]]:
    return partial(
        parse_generic_feed,
        provider=provider,
        publisher=publisher,
        primary=primary,
    )


def parse_nasa_rss(body: str) -> list[DiscoveryItem]:
    return parse_generic_feed(
        body,
        provider="nasa_rss",
        publisher="NASA",
        primary=True,
    )


def _timestamp_millis(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return _iso(datetime.fromtimestamp(value / 1000.0, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def parse_usgs_geojson(body: str) -> list[DiscoveryItem]:
    document = json.loads(body)
    if not isinstance(document, Mapping) or document.get("type") != "FeatureCollection":
        raise ValueError("USGS response must be a GeoJSON FeatureCollection")
    features = document.get("features")
    if not isinstance(features, list):
        raise ValueError("USGS response must contain a features array")
    items: list[DiscoveryItem] = []
    seen_urls: set[str] = set()
    for feature in features:
        if not isinstance(feature, Mapping):
            continue
        properties = feature.get("properties")
        if not isinstance(properties, Mapping):
            continue
        url = _valid_http_url(properties.get("url") or properties.get("detail"))
        if url is None or url in seen_urls:
            continue
        seen_urls.add(url)
        magnitude = properties.get("mag")
        place = _clean_text(properties.get("place"), limit=160) or "an unspecified location"
        title_value = properties.get("title")
        if not isinstance(title_value, str) or not title_value.strip():
            magnitude_label = f"M {magnitude:g}" if isinstance(magnitude, (int, float)) else "Earthquake"
            title_value = f"{magnitude_label} near {place}"
        title = _safe_title(title_value)
        coordinates = None
        geometry = feature.get("geometry")
        if isinstance(geometry, Mapping) and isinstance(geometry.get("coordinates"), list):
            coordinates = geometry["coordinates"]
        depth = coordinates[2] if coordinates and len(coordinates) >= 3 else None
        facts: list[str] = []
        if isinstance(magnitude, (int, float)) and not isinstance(magnitude, bool):
            facts.append(f"magnitude {magnitude:g}")
        facts.append(f"location {place}")
        if isinstance(depth, (int, float)) and not isinstance(depth, bool):
            facts.append(f"reported depth {depth:g} km")
        if properties.get("tsunami") == 1:
            facts.append("tsunami flag set in the USGS feed")
        summary = _safe_summary(
            "USGS reports " + ", ".join(facts) + ". Event details may be revised.",
            title,
        )
        items.append(
            DiscoveryItem(
                provider="usgs_significant_month",
                publisher="U.S. Geological Survey",
                title=title,
                summary=summary,
                url=url,
                published_at=_timestamp_millis(properties.get("time")),
                primary=True,
            )
        )
    if not items:
        raise ValueError("USGS GeoJSON contained no usable significant events")
    return items


def _wikimedia_page_url(item: Mapping[str, Any]) -> str | None:
    content_urls = item.get("content_urls")
    if isinstance(content_urls, Mapping):
        desktop = content_urls.get("desktop")
        if isinstance(desktop, Mapping):
            page = _valid_http_url(desktop.get("page"))
            if page:
                return page
    title = item.get("title")
    if isinstance(title, str) and title.strip():
        quoted = urllib.parse.quote(title.replace(" ", "_"), safe="_()")
        return f"https://en.wikipedia.org/wiki/{quoted}"
    return None


def parse_wikimedia_featured(body: str) -> list[DiscoveryItem]:
    document = json.loads(body)
    if not isinstance(document, Mapping):
        raise ValueError("Wikimedia response must be an object")

    raw_items: list[Mapping[str, Any]] = []
    featured = document.get("tfa")
    if isinstance(featured, Mapping):
        raw_items.append(featured)
    mostread = document.get("mostread")
    if isinstance(mostread, Mapping):
        articles = mostread.get("articles")
        if isinstance(articles, list):
            raw_items.extend(item for item in articles if isinstance(item, Mapping))

    seen_urls: set[str] = set()
    items: list[DiscoveryItem] = []
    for raw in raw_items:
        url = _wikimedia_page_url(raw)
        if url is None or url in seen_urls:
            continue
        seen_urls.add(url)
        title = _safe_title(
            raw.get("normalizedtitle") or raw.get("displaytitle") or raw.get("title")
        )
        summary = _safe_summary(raw.get("extract") or raw.get("description"), title)
        items.append(
            DiscoveryItem(
                provider="wikimedia_featured",
                publisher="Wikimedia Foundation",
                title=title,
                summary=summary,
                url=url,
                published_at=None,
                primary=False,
            )
        )
    if not items:
        raise ValueError("Wikimedia response contained no usable featured items")
    return items


def default_transport(url: str, timeout: float) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, application/rss+xml, application/xml;q=0.9, */*;q=0.1",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise OSError(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
        charset = response.headers.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")


class ScoutCache:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _path(self, source_id: str) -> Path:
        return self.directory / f"{source_id}.json"

    def load(self, source_id: str) -> CachedResponse | None:
        try:
            document = json.loads(self._path(source_id).read_text(encoding="utf-8"))
            if not isinstance(document, Mapping):
                return None
            return CachedResponse(
                source_id=str(document["source_id"]),
                endpoint=str(document["endpoint"]),
                fetched_at=str(document["fetched_at"]),
                body=str(document["body"]),
            )
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save(self, response: CachedResponse) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._path(response.source_id)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        payload = {
            "schema_version": "1.0.0",
            "source_id": response.source_id,
            "endpoint": response.endpoint,
            "fetched_at": response.fetched_at,
            "body": response.body,
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
            temporary.replace(target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _fallback_group(
    provider: str,
    publisher: str,
    records: Sequence[tuple[str, str, str]],
    *,
    primary: bool,
) -> list[DiscoveryItem]:
    return [
        DiscoveryItem(
            provider=provider,
            publisher=publisher,
            title=title,
            summary=summary,
            url=url,
            published_at=None,
            primary=primary,
        )
        for title, summary, url in records
    ]


def _bundled_fallbacks() -> dict[str, list[DiscoveryItem]]:
    return {
        "nasa_rss": _fallback_group(
            "nasa_rss",
            "NASA",
            (
                (
                    "Voyager mission overview",
                    "NASA documents the engineering discipline and team resilience that kept the Voyager probes operating far beyond their original mission.",
                    "https://science.nasa.gov/mission/voyager/",
                ),
                (
                    "James Webb Space Telescope mission",
                    "NASA's mission page explains the long-term teamwork, perseverance, and engineering discipline behind the Webb observatory.",
                    "https://science.nasa.gov/mission/webb/",
                ),
                (
                    "Hubble Space Telescope mission",
                    "NASA records how mission teams overcame an early engineering setback and restored Hubble through disciplined work.",
                    "https://science.nasa.gov/mission/hubble/",
                ),
                (
                    "Artemis lunar exploration",
                    "NASA's Artemis overview describes astronaut training, team discipline, and the programme's long-term exploration goals.",
                    "https://www.nasa.gov/humans-in-space/artemis/",
                ),
            ),
            primary=True,
        ),
        "wikimedia_featured": _fallback_group(
            "wikimedia_featured",
            "Wikimedia Foundation",
            (
                (
                    "Acupuncture evidence overview",
                    "Acupuncture is a practice associated with traditional Chinese medicine; efficacy and safety vary by condition and require medical review.",
                    "https://en.wikipedia.org/wiki/Acupuncture",
                ),
                (
                    "Battle of Midway",
                    "The Battle of Midway was a major naval battle in the Pacific theatre of World War II.",
                    "https://en.wikipedia.org/wiki/Battle_of_Midway",
                ),
                (
                    "David Goggins",
                    "David Goggins is an endurance athlete and public speaker whose work discusses discipline and mental toughness.",
                    "https://en.wikipedia.org/wiki/David_Goggins",
                ),
                (
                    "Hand washing",
                    "Hand washing is a public-health practice used to reduce the spread of infection and disease.",
                    "https://en.wikipedia.org/wiki/Hand_washing",
                ),
            ),
            primary=False,
        ),
        "noaa_oceanfacts": _fallback_group(
            "noaa_oceanfacts",
            "NOAA National Ocean Service",
            (
                (
                    "Why is the ocean blue?",
                    "NOAA explains how water absorbs longer wavelengths and scatters shorter blue wavelengths.",
                    "https://oceanservice.noaa.gov/facts/oceanblue.html",
                ),
                (
                    "Bioluminescence in the ocean",
                    "NOAA describes light produced by living organisms and why it is common in the ocean.",
                    "https://oceanservice.noaa.gov/facts/biolum.html",
                ),
                (
                    "How deep is the ocean?",
                    "NOAA summarizes average ocean depth and the much deeper trenches mapped below the surface.",
                    "https://oceanservice.noaa.gov/facts/oceandepth.html",
                ),
                (
                    "What causes tides?",
                    "NOAA explains the roles of lunar and solar gravity in the repeating rise and fall of sea level.",
                    "https://oceanservice.noaa.gov/education/tutorial_tides/tides02_cause.html",
                ),
            ),
            primary=True,
        ),
        "noaa_nosnews": _fallback_group(
            "noaa_nosnews",
            "NOAA National Ocean Service",
            (
                (
                    "NOAA National Ocean Service news",
                    "NOAA publishes current reporting about ocean observations, coasts, navigation, and marine ecosystems.",
                    "https://oceanservice.noaa.gov/news/",
                ),
                (
                    "How NOAA studies coral reefs",
                    "NOAA describes coral reef monitoring, ecosystem services, threats, and restoration work.",
                    "https://oceanservice.noaa.gov/ecosystems/coralreef/",
                ),
                (
                    "NOAA hurricane ocean services",
                    "NOAA documents coastal observations, mapping, and response information used around hurricanes.",
                    "https://oceanservice.noaa.gov/hazards/hurricanes/",
                ),
            ),
            primary=True,
        ),
        "noaa_nosmedia": _fallback_group(
            "noaa_nosmedia",
            "NOAA National Ocean Service",
            (
                (
                    "National Ocean Service newsroom",
                    "NOAA's newsroom publishes official releases and media resources about oceans and coasts.",
                    "https://oceanservice.noaa.gov/newsroom/",
                ),
                (
                    "What is coral bleaching?",
                    "NOAA explains how stressed corals expel algae, lose colour, and become more vulnerable.",
                    "https://oceanservice.noaa.gov/facts/coral_bleach.html",
                ),
                (
                    "What is a dead zone?",
                    "NOAA explains low-oxygen areas in water and the conditions that can create them.",
                    "https://oceanservice.noaa.gov/facts/deadzone.html",
                ),
            ),
            primary=True,
        ),
        "usgs_significant_month": _fallback_group(
            "usgs_significant_month",
            "U.S. Geological Survey",
            (
                (
                    "How earthquake magnitude works",
                    "USGS explains the relationship between earthquake magnitude, released energy, and shaking intensity.",
                    "https://www.usgs.gov/programs/earthquake-hazards/science/earthquake-magnitude-energy-release-and-shaking-intensity",
                ),
                (
                    "Aftershock forecasting",
                    "USGS describes how scientists estimate the changing probability of aftershocks after an earthquake.",
                    "https://www.usgs.gov/programs/earthquake-hazards/science/aftershock-forecasting",
                ),
                (
                    "Earthquake early warning",
                    "USGS explains how sensor networks can detect an earthquake and issue alerts before strong shaking arrives farther away.",
                    "https://www.usgs.gov/programs/earthquake-hazards/science/earthquake-early-warning",
                ),
                (
                    "Cool earthquake facts",
                    "USGS collects verified introductory facts about earthquakes, faults, seismic waves, and global monitoring.",
                    "https://www.usgs.gov/programs/earthquake-hazards/science/cool-earthquake-facts",
                ),
            ),
            primary=True,
        ),
        "esa_space_science": _fallback_group(
            "esa_space_science",
            "European Space Agency",
            (
                (
                    "Gaia star-mapping mission",
                    "ESA's Gaia mission measured positions and motions to build a detailed map of the Milky Way.",
                    "https://www.esa.int/Science_Exploration/Space_Science/Gaia",
                ),
                (
                    "Euclid dark-universe mission",
                    "ESA's Euclid mission studies the geometry and large-scale structure of the dark Universe.",
                    "https://www.esa.int/Science_Exploration/Space_Science/Euclid",
                ),
                (
                    "Juice mission to Jupiter",
                    "ESA's Juice mission investigates Jupiter and three large icy moons as planetary systems and possible habitats.",
                    "https://www.esa.int/Science_Exploration/Space_Science/Juice",
                ),
                (
                    "ESA Webb science",
                    "ESA documents its partnership and scientific contributions to the James Webb Space Telescope.",
                    "https://www.esa.int/Science_Exploration/Space_Science/Webb",
                ),
            ),
            primary=True,
        ),
        "loc_latest_news": _fallback_group(
            "loc_latest_news",
            "Library of Congress",
            (
                (
                    "Library of Congress digital collections",
                    "The Library of Congress provides access to digitized photographs, recordings, maps, manuscripts, and books.",
                    "https://www.loc.gov/collections/",
                ),
                (
                    "National Film Registry",
                    "The Library of Congress maintains the National Film Registry and publishes material about notable films, actors, and directors.",
                    "https://www.loc.gov/programs/national-film-preservation-board/film-registry/",
                ),
                (
                    "Library exhibitions",
                    "Library of Congress exhibitions interpret collection items and stories from United States and world history.",
                    "https://www.loc.gov/exhibits/",
                ),
            ),
            primary=True,
        ),
        "loc_folklife": _fallback_group(
            "loc_folklife",
            "Library of Congress American Folklife Center",
            (
                (
                    "American life histories",
                    "The Library preserves thousands of life-history interviews created by the Federal Writers' Project in the 1930s.",
                    "https://www.loc.gov/collections/federal-writers-project/about-this-collection/",
                ),
                (
                    "California folk music from the 1930s",
                    "The Library collection documents multilingual folk music recorded in Northern California in the late 1930s.",
                    "https://www.loc.gov/collections/california-gold-northern-california-folk-music-from-the-thirties/about-this-collection/",
                ),
                (
                    "Alan Lomax collection",
                    "The Library preserves manuscripts and field materials created during folklorist Alan Lomax's collecting career.",
                    "https://www.loc.gov/collections/alan-lomax-manuscripts/about-this-collection/",
                ),
                (
                    "Occupational Folklife Project",
                    "The Library's Occupational Folklife Project records first-person accounts of working life across the United States.",
                    "https://www.loc.gov/collections/occupational-folklife-project/about-this-collection/",
                ),
            ),
            primary=True,
        ),
    }


PRODUCTION_LANES = (
    "war_history",
    "celebrity_news",
    "motivation",
    "chinese_medicine",
    "health",
)

# Discovery is deliberately evidence-led.  A provider is never a topic: an
# unrelated NASA, NOAA or Library item must not be forced into a production
# lane merely because the old three-pod runtime needed a non-null value.
_TOKEN_WEIGHTS: dict[str, dict[str, int]] = {
    "war_history": {
        "army": 3,
        "battle": 4,
        "campaign": 2,
        "combat": 3,
        "commander": 3,
        "conflict": 2,
        "invasion": 4,
        "military": 3,
        "naval": 3,
        "navy": 3,
        "soldier": 3,
        "soldiers": 3,
        "veteran": 2,
        "veterans": 2,
        "war": 4,
        "wartime": 4,
        "война": 4,
        "битва": 4,
        "военный": 3,
    },
    "celebrity_news": {
        "actor": 3,
        "actors": 3,
        "actress": 3,
        "actresses": 3,
        "celebrity": 4,
        "director": 2,
        "directors": 2,
        "entertainer": 3,
        "filmmaker": 3,
        "musician": 3,
        "performer": 3,
        "rapper": 3,
        "singer": 3,
        "star": 2,
        "звезда": 4,
        "актер": 3,
        "актриса": 3,
        "певец": 3,
        "певица": 3,
    },
    "motivation": {
        "achievement": 3,
        "discipline": 4,
        "endurance": 3,
        "grit": 4,
        "mindset": 4,
        "motivation": 4,
        "perseverance": 4,
        "resilience": 4,
        "overcome": 3,
        "success": 3,
        "дисциплина": 4,
        "мотивация": 4,
        "стойкость": 4,
        "успех": 3,
    },
    "chinese_medicine": {
        "acupuncture": 5,
        "acupressure": 5,
        "cupping": 5,
        "moxibustion": 5,
        "qigong": 5,
        "tuina": 5,
        "акупунктура": 5,
        "иглоукалывание": 5,
        "цигун": 5,
    },
    "health": {
        "clinical": 2,
        "disease": 3,
        "health": 3,
        "illness": 3,
        "infection": 3,
        "medical": 3,
        "medicine": 3,
        "patient": 2,
        "patients": 2,
        "prevention": 3,
        "symptom": 3,
        "symptoms": 3,
        "treatment": 2,
        "здоровье": 3,
        "болезнь": 3,
        "лечение": 2,
        "медицина": 3,
    },
}

_PHRASE_WEIGHTS: dict[str, dict[str, int]] = {
    "war_history": {
        "war history": 6,
        "world war": 6,
        "military history": 6,
        "oral history veteran": 5,
        "история войны": 6,
    },
    "celebrity_news": {
        "celebrity news": 6,
        "film star": 5,
        "music star": 5,
        "новости звезд": 6,
    },
    "motivation": {
        "mental toughness": 6,
        "personal growth": 5,
        "work ethic": 6,
        "overcame adversity": 6,
        "личный рост": 5,
    },
    "chinese_medicine": {
        "chinese medicine": 7,
        "traditional chinese medicine": 8,
        "chinese herbal": 7,
        "китайская медицина": 8,
    },
    "health": {
        "public health": 6,
        "health care": 5,
        "healthcare": 5,
        "medical study": 5,
        "disease prevention": 6,
        "общественное здоровье": 6,
    },
}

_WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


def _topic_for(item: DiscoveryItem) -> str | None:
    tokens = [token.casefold() for token in _WORD_RE.findall(f"{item.title} {item.summary}")]
    token_set = set(tokens)
    normalized = f" {' '.join(tokens)} "
    scores = {lane: 0 for lane in PRODUCTION_LANES}
    for lane, weights in _TOKEN_WEIGHTS.items():
        scores[lane] += sum(weight for token, weight in weights.items() if token in token_set)
        scores[lane] += sum(
            weight
            for phrase, weight in _PHRASE_WEIGHTS[lane].items()
            if f" {phrase} " in normalized
        )
    best_score = max(scores.values())
    # A single weak keyword such as "star" or "treatment" is not sufficient
    # evidence for routing.  Ambiguous ties also stay out of production.
    if best_score < 3:
        return None
    leaders = [lane for lane in PRODUCTION_LANES if scores[lane] == best_score]
    return leaders[0] if len(leaders) == 1 else None


def _stable_id(item: DiscoveryItem) -> str:
    digest = hashlib.sha256(f"{item.provider}\0{item.url}".encode("utf-8")).hexdigest()[:16]
    return f"scout_{item.provider}_{digest}"


def _source_id(item: DiscoveryItem) -> str:
    digest = hashlib.sha256(item.url.encode("utf-8")).hexdigest()[:12]
    return f"src_{item.provider}_{digest}"


def _why_now(item: DiscoveryItem, production_date: date, freshness: str) -> str:
    if item.published_at:
        return _clean_text(
            f"Fresh official-source item published {item.published_at[:10]}; discovered for the "
            f"{production_date.isoformat()} production slate.",
            limit=300,
        )
    if freshness == "bundled":
        return "Evergreen offline fallback; refresh the source before topic approval."
    return _clean_text(
        f"Featured by {item.publisher} for the {production_date.isoformat()} discovery run.",
        limit=300,
    )


def _score(item: DiscoveryItem, freshness: str) -> dict[str, float]:
    live = freshness == "live"
    return {
        "relevance": 0.78 if live else 0.50,
        "trend_acceleration": 0.72 if item.published_at and live else 0.35,
        "audience_fit": 0.72,
        "visualability": 0.68,
        "source_quality": 0.92 if item.primary else 0.65,
        "rights_availability": 0.45,
        "novelty": 0.60,
        "saturation": 0.45,
        "policy_risk": 0.20 if item.primary else 0.30,
    }


def _normalize_item(
    item: DiscoveryItem,
    *,
    lane: str,
    retrieved_at: str,
    production_date: date,
    freshness: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    idea_id = _stable_id(item)
    source_id = _source_id(item)
    title = _safe_title(item.title)
    message = _safe_summary(item.summary, title)
    hook = _clean_text(
        f"Почему история «{title}» важнее, чем кажется по заголовку?",
        limit=180,
    )
    rights_note = "Discovery URL is evidence only. Media reuse rights have not been checked."
    idea_card = {
        "schema_version": "1.0.0",
        "idea_id": idea_id,
        "pod": lane,
        "title": title,
        "hook": hook,
        "message": message,
        "why_now": _why_now(item, production_date, freshness),
        "audience": "Русскоязычная аудитория 18–44 лет, короткие вертикальные видео",
        "destination": ["youtube_shorts", "instagram_reels", "tiktok"],
        "source_candidates": [
            {
                "source_id": source_id,
                "url": item.url,
                "publisher": item.publisher,
                "source_type": "primary" if item.primary else "official",
                "notes": rights_note,
            }
        ],
        "visual_plan": {
            "target_shots": 10,
            "rights_feasibility": "yellow",
            "visual_world": f"Real, source-relevant footage and archive imagery for {title}",
            "candidate_asset_count": 0,
        },
        "score": _score(item, freshness),
        "risk": "yellow",
        "status": "candidate",
        "created_at": retrieved_at,
    }
    review_notes = [
        "Discovery-stage source only; a research agent must verify wording and corroboration.",
        rights_note,
    ]
    if freshness != "live":
        review_notes.append(f"Source response came from {freshness} fallback and must be refreshed.")
    claim_ledger = {
        "schema_version": "1.0.0",
        "idea_id": idea_id,
        "sources": [
            {
                "source_id": source_id,
                "url": item.url,
                "publisher": item.publisher,
                "retrieved_at": retrieved_at,
                "primary": item.primary,
                "archived_receipt": None,
                "notes": (
                    f"Discovery feed item; published_at={item.published_at or 'unknown'}; "
                    f"response_freshness={freshness}."
                ),
            }
        ],
        "claims": [
            {
                "claim_id": "CLM-001",
                "text": message,
                "source_ids": [source_id],
                "support": "direct",
                "risk": "yellow",
                "script_usage": "qualify",
                "notes": "Directly quoted or summarized from one discovery item; not corroborated.",
            }
        ],
        "decision": {
            "passed": False,
            "needs_human_review": True,
            "review_notes": review_notes,
        },
    }
    provenance = {
        "idea_id": idea_id,
        "provider": item.provider,
        "freshness": freshness,
        "published_at": item.published_at,
        "discovered_url": item.url,
    }
    return idea_card, claim_ledger, provenance


def _round_robin(groups: Sequence[Sequence[DiscoveryItem]], limit: int) -> list[DiscoveryItem]:
    output: list[DiscoveryItem] = []
    seen_urls: set[str] = set()
    index = 0
    while len(output) < limit:
        added = False
        for group in groups:
            if index < len(group):
                added = True
                item = group[index]
                normalized_url = item.url.rstrip("/").casefold()
                if normalized_url in seen_urls:
                    continue
                seen_urls.add(normalized_url)
                output.append(item)
                if len(output) >= limit:
                    return output
        if not added:
            break
        index += 1
    return output


def _source_definitions(production_date: date) -> list[SourceDefinition]:
    return [
        SourceDefinition("nasa_rss", "NASA", NASA_RSS_URL, parse_nasa_rss),
        SourceDefinition(
            "wikimedia_featured",
            "Wikimedia Foundation",
            WIKIMEDIA_FEATURED_URL.format(
                year=production_date.strftime("%Y"),
                month=production_date.strftime("%m"),
                day=production_date.strftime("%d"),
            ),
            parse_wikimedia_featured,
        ),
        SourceDefinition(
            "noaa_oceanfacts",
            "NOAA National Ocean Service",
            NOAA_OCEANFACTS_URL,
            generic_feed_parser("noaa_oceanfacts", "NOAA National Ocean Service"),
        ),
        SourceDefinition(
            "noaa_nosnews",
            "NOAA National Ocean Service",
            NOAA_NOSNEWS_URL,
            generic_feed_parser("noaa_nosnews", "NOAA National Ocean Service"),
        ),
        SourceDefinition(
            "noaa_nosmedia",
            "NOAA National Ocean Service",
            NOAA_NOSMEDIA_URL,
            generic_feed_parser("noaa_nosmedia", "NOAA National Ocean Service"),
        ),
        SourceDefinition(
            "usgs_significant_month",
            "U.S. Geological Survey",
            USGS_SIGNIFICANT_MONTH_URL,
            parse_usgs_geojson,
        ),
        SourceDefinition(
            "esa_space_science",
            "European Space Agency",
            ESA_SPACE_SCIENCE_URL,
            generic_feed_parser("esa_space_science", "European Space Agency"),
        ),
        SourceDefinition(
            "loc_latest_news",
            "Library of Congress",
            LOC_LATEST_NEWS_URL,
            generic_feed_parser("loc_latest_news", "Library of Congress"),
        ),
        SourceDefinition(
            "loc_folklife",
            "Library of Congress American Folklife Center",
            LOC_FOLKLIFE_URL,
            generic_feed_parser(
                "loc_folklife", "Library of Congress American Folklife Center"
            ),
        ),
    ]


def _parse_cached_time(value: str, now: datetime) -> float | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _fetch_source(
    source: SourceDefinition,
    *,
    cache: ScoutCache,
    transport: Transport,
    timeout: float,
    retries: int,
    offline: bool,
    now: datetime,
) -> tuple[list[DiscoveryItem], dict[str, Any], list[str]]:
    errors: list[str] = []
    if not offline:
        for attempt in range(retries + 1):
            try:
                body = transport(source.endpoint, timeout)
                items = source.parser(body)
                retrieved_at = _iso(now)
                try:
                    cache.save(
                        CachedResponse(source.source_id, source.endpoint, retrieved_at, body)
                    )
                except OSError as exc:
                    errors.append(f"cache write failed: {type(exc).__name__}: {exc}")
                return items, {
                    "source_id": source.source_id,
                    "publisher": source.publisher,
                    "endpoint": source.endpoint,
                    "mode": "live",
                    "retrieved_at": retrieved_at,
                    "cache_age_seconds": 0,
                    "item_count": len(items),
                }, errors
            except (OSError, TimeoutError, ValueError, ET.ParseError, json.JSONDecodeError) as exc:
                errors.append(f"attempt {attempt + 1}: {type(exc).__name__}: {exc}")

    cached = cache.load(source.source_id)
    if cached is not None:
        try:
            items = source.parser(cached.body)
            return items, {
                "source_id": source.source_id,
                "publisher": source.publisher,
                "endpoint": cached.endpoint,
                "mode": "cache",
                "retrieved_at": cached.fetched_at,
                "cache_age_seconds": _parse_cached_time(cached.fetched_at, now),
                "item_count": len(items),
            }, errors
        except (ValueError, ET.ParseError, json.JSONDecodeError) as exc:
            errors.append(f"invalid cache: {type(exc).__name__}: {exc}")

    items = _bundled_fallbacks()[source.source_id]
    errors.append("using bundled evergreen fallback; refresh required before approval")
    return items, {
        "source_id": source.source_id,
        "publisher": source.publisher,
        "endpoint": source.endpoint,
        "mode": "bundled",
        "retrieved_at": _iso(now),
        "cache_age_seconds": None,
        "item_count": len(items),
    }, errors


def run_scout(
    *,
    production_date: date | str | None = None,
    limit: int = 12,
    cache_dir: str | Path = ".video-factory-cache/scout",
    timeout: float = 8.0,
    retries: int = 1,
    offline: bool = False,
    lanes: Sequence[str] | None = None,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if isinstance(production_date, str):
        try:
            production_date = date.fromisoformat(production_date)
        except ValueError as exc:
            raise ValidationError("production_date must use YYYY-MM-DD") from exc
    if production_date is None:
        production_date = date.today()
    if not isinstance(production_date, date):
        raise ValidationError("production_date must be a date or YYYY-MM-DD")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValidationError("limit must be an integer from 1 to 50")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 60:
        raise ValidationError("timeout must be between 0.1 and 60 seconds")
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 3:
        raise ValidationError("retries must be an integer from 0 to 3")
    if lanes is None:
        requested_lanes = PRODUCTION_LANES
    else:
        if isinstance(lanes, (str, bytes)) or not isinstance(lanes, Sequence):
            raise ValidationError("lanes must be a sequence of production lane IDs")
        requested_lanes = tuple(dict.fromkeys(lanes))
        if not requested_lanes:
            raise ValidationError("lanes must contain at least one production lane ID")
        invalid_lanes = [lane for lane in requested_lanes if lane not in PRODUCTION_LANES]
        if invalid_lanes:
            raise ValidationError(
                "unsupported production lanes: " + ", ".join(invalid_lanes)
            )

    now = now or _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cache = ScoutCache(cache_dir)
    transport = transport or default_transport
    groups: list[list[DiscoveryItem]] = []
    source_reports: list[dict[str, Any]] = []
    warnings: list[str] = []

    for source in _source_definitions(production_date):
        items, report, errors = _fetch_source(
            source,
            cache=cache,
            transport=transport,
            timeout=float(timeout),
            retries=retries,
            offline=offline,
            now=now,
        )
        groups.append(items)
        report["errors"] = errors
        source_reports.append(report)
        warnings.extend(f"{source.source_id}: {error}" for error in errors)

    lane_groups: dict[str, list[DiscoveryItem]] = {
        lane: [] for lane in requested_lanes
    }
    unmatched_item_count = 0
    excluded_by_lane_filter = 0
    for group in groups:
        for item in group:
            lane = _topic_for(item)
            if lane is None:
                unmatched_item_count += 1
            elif lane not in lane_groups:
                excluded_by_lane_filter += 1
            else:
                lane_groups[lane].append(item)

    # Round-robin by production lane, not by provider, so a noisy feed cannot
    # starve the smaller editorial cells of candidate slots.
    selected = _round_robin(
        [lane_groups[lane] for lane in requested_lanes],
        limit,
    )
    source_modes = {report["source_id"]: report["mode"] for report in source_reports}
    source_times = {report["source_id"]: report["retrieved_at"] for report in source_reports}
    ideas: list[dict[str, Any]] = []
    ledgers: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    selected_by_lane = {lane: 0 for lane in requested_lanes}
    for item in selected:
        lane = _topic_for(item)
        if lane is None or lane not in lane_groups:
            raise RuntimeError("selected discovery item lost its validated lane")
        freshness = source_modes[item.provider]
        idea, ledger, trace = _normalize_item(
            item,
            lane=lane,
            retrieved_at=source_times[item.provider],
            production_date=production_date,
            freshness=freshness,
        )
        ideas.append(idea)
        ledgers.append(ledger)
        provenance.append(trace)
        selected_by_lane[lane] += 1

    modes = set(source_modes.values())
    overall_mode = next(iter(modes)) if len(modes) == 1 else "mixed"
    return {
        "schema_version": "1.0.0",
        "ok": True,
        "generated_at": _iso(now),
        "production_date": production_date.isoformat(),
        "mode": overall_mode,
        "offline_requested": offline,
        "requested_lanes": list(requested_lanes),
        "lane_coverage": {
            lane: {
                "available_candidates": len(lane_groups[lane]),
                "selected_candidates": selected_by_lane[lane],
                "covered": bool(lane_groups[lane]),
            }
            for lane in requested_lanes
        },
        "unmatched_item_count": unmatched_item_count,
        "excluded_by_lane_filter": excluded_by_lane_filter,
        "candidate_count": len(ideas),
        "ideas": ideas,
        "claim_ledgers": ledgers,
        "provenance": provenance,
        "sources": source_reports,
        "warnings": warnings,
        "human_gate_required": True,
    }
