from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

FACTORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FACTORY_ROOT / "src"))

from video_factory.cli import main  # noqa: E402
from video_factory.contracts import validate_artifact  # noqa: E402
from video_factory.errors import ValidationError  # noqa: E402
from video_factory.scout import (  # noqa: E402
    DiscoveryItem,
    _topic_for,
    parse_generic_feed,
    parse_usgs_geojson,
    run_scout,
)


NASA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>NASA Finds an Unexpected Moon Signal</title>
    <link>https://www.nasa.gov/example/moon-signal/</link>
    <description><![CDATA[<p>A NASA instrument recorded a new signal during its lunar survey.</p>]]></description>
    <pubDate>Wed, 26 Aug 2026 12:00:00 +0000</pubDate>
  </item>
  <item>
    <title>NASA Earth Science Maps Ocean Currents</title>
    <link>https://www.nasa.gov/example/earth-ocean-currents/</link>
    <description>Earth science instruments mapped changing ocean currents and marine conditions.</description>
    <pubDate>Tue, 25 Aug 2026 12:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Engineers Test a Deep-Space Antenna</title>
    <link>https://www.nasa.gov/example/deep-space-antenna/</link>
    <description>Engineers completed a test of a new antenna for a future mission.</description>
    <pubDate>Mon, 24 Aug 2026 12:00:00 +0000</pubDate>
  </item>
  <item>
    <title>New Telescope Studies a Distant Galaxy</title>
    <link>https://www.nasa.gov/example/telescope-galaxy/</link>
    <description>A space telescope observed stars in a distant galaxy.</description>
    <pubDate>Sun, 23 Aug 2026 12:00:00 +0000</pubDate>
  </item>
</channel></rss>"""

WIKIMEDIA_JSON = json.dumps(
    {
        "tfa": {
            "title": "Acupuncture",
            "normalizedtitle": "Acupuncture evidence",
            "extract": "Acupuncture is associated with traditional Chinese medicine and requires evidence-based medical review.",
            "content_urls": {
                "desktop": {"page": "https://en.wikipedia.org/wiki/Acupuncture"}
            },
        },
        "mostread": {
            "articles": [
                {
                    "title": "Lord_Mountbatten",
                    "normalizedtitle": "Lord Mountbatten",
                    "extract": "Lord Mountbatten was a British statesman and naval officer.",
                    "content_urls": {
                        "desktop": {
                            "page": "https://en.wikipedia.org/wiki/Lord_Mountbatten"
                        }
                    },
                },
                {
                    "title": "Film_actor",
                    "normalizedtitle": "Film actor profile",
                    "extract": "A celebrity actor and director received a major film award.",
                    "content_urls": {
                        "desktop": {
                            "page": "https://en.wikipedia.org/wiki/Film_actor"
                        }
                    },
                },
                {
                    "title": "Resilience",
                    "normalizedtitle": "Resilience and discipline",
                    "extract": "A personal growth story about discipline, resilience, and overcoming adversity.",
                    "content_urls": {
                        "desktop": {"page": "https://en.wikipedia.org/wiki/Psychological_resilience"}
                    },
                },
            ]
        },
    }
)


def rss_fixture(slug: str, count: int = 4) -> str:
    topics = (
        ("World War battle archive", "A primary military history source documents a battle and its wartime context."),
        ("Celebrity actor interview", "An official archive profiles a celebrity actor, singer, and director."),
        ("Discipline and resilience", "An official account documents perseverance, discipline, and personal growth."),
        ("Public health prevention", "An official health source explains disease and infection prevention."),
    )
    items = "".join(
        f"""
        <item>
          <title>{topics[(index - 1) % len(topics)][0]} {index}</title>
          <link>https://example.test/{slug}/{index}</link>
          <description>{topics[(index - 1) % len(topics)][1]}</description>
          <pubDate>Wed, {20 + index} Aug 2026 09:00:00 +0000</pubDate>
        </item>"""
        for index in range(1, count + 1)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{items}</channel></rss>'


USGS_JSON = json.dumps(
    {
        "type": "FeatureCollection",
        "metadata": {"count": 4},
        "features": [
            {
                "type": "Feature",
                "id": f"event-{index}",
                "properties": {
                    "mag": 5.5 + index / 10,
                    "place": f"{10 * index} km from Test Coast {index}",
                    "time": 1787745600000 + index * 1000,
                    "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/test{index}",
                    "title": f"M {5.5 + index / 10:.1f} - Test Coast {index}",
                    "tsunami": 1 if index == 1 else 0,
                },
                "geometry": {"type": "Point", "coordinates": [1, 2, 10 + index]},
            }
            for index in range(1, 5)
        ],
    }
)


def fake_transport(url: str, timeout: float) -> str:
    if url == "https://www.nasa.gov/news-release/feed/":
        return NASA_XML
    if "api.wikimedia.org" in url:
        return WIKIMEDIA_JSON
    if "significant_month.geojson" in url:
        return USGS_JSON
    if "oceanfacts.xml" in url:
        return rss_fixture("noaa-oceanfacts")
    if "nosnews.xml" in url:
        return rss_fixture("noaa-nosnews")
    if "nosmedia.xml" in url:
        return rss_fixture("noaa-nosmedia")
    if "esa.int/rssfeed" in url:
        return rss_fixture("esa-space-science")
    if "rss/pao/news.xml" in url:
        return rss_fixture("loc-latest-news")
    if "blogs.loc.gov/folklife/feed" in url:
        return rss_fixture("loc-folklife")
    raise AssertionError(f"unexpected URL: {url}")


def item(provider: str, title: str, summary: str = "A sufficiently detailed summary.") -> DiscoveryItem:
    return DiscoveryItem(
        provider=provider,
        publisher="Test publisher",
        title=title,
        summary=summary,
        url=f"https://example.test/{provider}/{title.replace(' ', '-')}",
        published_at=None,
        primary=True,
    )


class ScoutTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 8, 27, 10, 30, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_generic_parser_handles_rss_and_namespaced_atom(self) -> None:
        rss = parse_generic_feed(
            rss_fixture("generic-rss", count=1),
            provider="test_rss",
            publisher="Test RSS",
            primary=True,
        )
        self.assertEqual(len(rss), 1)
        self.assertEqual(rss[0].url, "https://example.test/generic-rss/1")
        self.assertEqual(rss[0].published_at, "2026-08-21T09:00:00Z")

        atom = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>One Atom Discovery</title>
            <link rel="alternate" href="https://example.test/atom/1" />
            <summary>Official Atom summary with enough factual context.</summary>
            <updated>2026-08-27T08:15:00Z</updated>
          </entry>
        </feed>"""
        parsed_atom = parse_generic_feed(
            atom,
            provider="test_atom",
            publisher="Test Atom",
            primary=True,
        )
        self.assertEqual(len(parsed_atom), 1)
        self.assertEqual(parsed_atom[0].title, "One Atom Discovery")
        self.assertEqual(parsed_atom[0].published_at, "2026-08-27T08:15:00Z")

    def test_generic_parser_recovers_only_well_formed_rss_items(self) -> None:
        malformed = """<rss><channel>
          <item><title>Broken entry</item>
          <item>
            <title>Valid official discovery</title>
            <link>https://example.test/valid</link>
            <description>A complete and independently well-formed source summary.</description>
          </item>
        </channel></rss>"""
        parsed = parse_generic_feed(
            malformed,
            provider="test_recovery",
            publisher="Test publisher",
            primary=True,
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].title, "Valid official discovery")

    def test_usgs_geojson_parser_preserves_event_facts(self) -> None:
        parsed = parse_usgs_geojson(USGS_JSON)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(parsed[0].provider, "usgs_significant_month")
        self.assertIn("magnitude 5.6", parsed[0].summary)
        self.assertIn("reported depth 11 km", parsed[0].summary)
        self.assertIn("tsunami flag", parsed[0].summary)
        self.assertTrue(parsed[0].url.startswith("https://earthquake.usgs.gov/"))

    def test_live_sources_produce_contract_valid_pool_of_28(self) -> None:
        result = run_scout(
            production_date="2026-08-27",
            limit=28,
            cache_dir=self.root,
            timeout=1,
            retries=0,
            transport=fake_transport,
            now=self.now,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["candidate_count"], 28)
        self.assertEqual(
            set(result["lane_coverage"]),
            {
                "war_history",
                "celebrity_news",
                "motivation",
                "chinese_medicine",
                "health",
            },
        )
        self.assertTrue(all(entry["covered"] for entry in result["lane_coverage"].values()))
        providers = {entry["source_id"] for entry in result["sources"]}
        self.assertEqual(
            providers,
            {
                "nasa_rss",
                "wikimedia_featured",
                "noaa_oceanfacts",
                "noaa_nosnews",
                "noaa_nosmedia",
                "usgs_significant_month",
                "esa_space_science",
                "loc_latest_news",
                "loc_folklife",
            },
        )
        self.assertEqual(len(list(self.root.glob("*.json"))), 9)
        for idea_card, ledger in zip(
            result["ideas"], result["claim_ledgers"], strict=True
        ):
            validate_artifact("idea_card", idea_card)
            validate_artifact("claim_ledger", ledger)
            self.assertEqual(idea_card["idea_id"], ledger["idea_id"])
            self.assertIn(idea_card["pod"], result["requested_lanes"])
            self.assertTrue(idea_card["hook"].startswith("Почему"))
            self.assertTrue(idea_card["audience"].startswith("Русскоязычная"))
            self.assertFalse(ledger["decision"]["passed"])
            self.assertTrue(ledger["decision"]["needs_human_review"])
            self.assertEqual(ledger["claims"][0]["script_usage"], "qualify")

        repeat = run_scout(
            production_date="2026-08-27",
            limit=28,
            cache_dir=self.root,
            retries=0,
            transport=fake_transport,
            now=datetime(2026, 8, 27, 11, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [idea_card["idea_id"] for idea_card in result["ideas"]],
            [idea_card["idea_id"] for idea_card in repeat["ideas"]],
        )

    def test_routing_is_fail_closed_and_only_returns_production_lanes(self) -> None:
        self.assertEqual(
            _topic_for(item("wikimedia_featured", "World War naval battle archive")),
            "war_history",
        )
        self.assertEqual(
            _topic_for(item("loc_latest_news", "Celebrity actor receives film award")),
            "celebrity_news",
        )
        self.assertEqual(
            _topic_for(item("loc_folklife", "Discipline, resilience, and personal growth")),
            "motivation",
        )
        self.assertEqual(
            _topic_for(item("wikimedia_featured", "Traditional Chinese medicine acupuncture")),
            "chinese_medicine",
        )
        self.assertEqual(
            _topic_for(item("noaa_nosnews", "Public health disease prevention")),
            "health",
        )
        self.assertIsNone(_topic_for(item("nasa_rss", "A New Exoplanet Telescope")))
        self.assertIsNone(_topic_for(item("loc_latest_news", "Routine agency update")))

    def test_network_failure_uses_previous_cache_for_every_source(self) -> None:
        run_scout(
            production_date="2026-08-27",
            limit=28,
            cache_dir=self.root,
            transport=fake_transport,
            retries=0,
            now=self.now,
        )
        calls: list[tuple[str, float]] = []

        def timeout_transport(url: str, timeout: float) -> str:
            calls.append((url, timeout))
            raise TimeoutError("simulated timeout")

        result = run_scout(
            production_date="2026-08-27",
            limit=28,
            cache_dir=self.root,
            timeout=0.25,
            retries=1,
            transport=timeout_transport,
            now=datetime(2026, 8, 27, 12, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(result["mode"], "cache")
        self.assertEqual(len(calls), 18)
        self.assertEqual({source["mode"] for source in result["sources"]}, {"cache"})
        self.assertTrue(
            all(source["cache_age_seconds"] == 7200 for source in result["sources"])
        )
        self.assertTrue(any("simulated timeout" in warning for warning in result["warnings"]))

    def test_offline_mode_never_calls_transport(self) -> None:
        run_scout(
            production_date="2026-08-27",
            limit=28,
            cache_dir=self.root,
            transport=fake_transport,
            retries=0,
            now=self.now,
        )

        def forbidden_transport(url: str, timeout: float) -> str:
            raise AssertionError("offline mode called the network transport")

        result = run_scout(
            production_date="2026-08-27",
            limit=28,
            cache_dir=self.root,
            offline=True,
            transport=forbidden_transport,
            now=self.now,
        )
        self.assertTrue(result["offline_requested"])
        self.assertEqual(result["mode"], "cache")
        self.assertEqual(result["candidate_count"], 28)

    def test_empty_cache_bundled_fallback_is_safe_and_covers_five_lanes(self) -> None:
        result = run_scout(
            production_date="2026-08-27",
            limit=50,
            cache_dir=self.root,
            offline=True,
            transport=lambda *_: (_ for _ in ()).throw(AssertionError("network call")),
            now=self.now,
        )
        self.assertEqual(result["mode"], "bundled")
        self.assertGreaterEqual(result["candidate_count"], 5)
        self.assertEqual(len(result["sources"]), 9)
        self.assertTrue(all(item["freshness"] == "bundled" for item in result["provenance"]))
        self.assertTrue(all(entry["covered"] for entry in result["lane_coverage"].values()))
        self.assertTrue(
            all(ledger["decision"]["passed"] is False for ledger in result["claim_ledgers"])
        )
        self.assertTrue(result["warnings"])

    def test_lane_filter_limits_output_without_rerouting_unmatched_items(self) -> None:
        result = run_scout(
            production_date="2026-08-27",
            limit=12,
            lanes=["health", "chinese_medicine"],
            cache_dir=self.root,
            retries=0,
            transport=fake_transport,
            now=self.now,
        )
        self.assertEqual(result["requested_lanes"], ["health", "chinese_medicine"])
        self.assertEqual(set(result["lane_coverage"]), {"health", "chinese_medicine"})
        self.assertEqual({idea["pod"] for idea in result["ideas"]}, {"health", "chinese_medicine"})
        self.assertGreater(result["excluded_by_lane_filter"], 0)
        self.assertGreater(result["unmatched_item_count"], 0)

    def test_validation_rejects_unbounded_inputs(self) -> None:
        with self.assertRaises(ValidationError):
            run_scout(limit=0, cache_dir=self.root, offline=True)
        with self.assertRaises(ValidationError):
            run_scout(timeout=0, cache_dir=self.root, offline=True)
        with self.assertRaises(ValidationError):
            run_scout(retries=4, cache_dir=self.root, offline=True)
        with self.assertRaises(ValidationError):
            run_scout(production_date="27-08-2026", cache_dir=self.root, offline=True)
        with self.assertRaises(ValidationError):
            run_scout(lanes=[], cache_dir=self.root, offline=True)
        with self.assertRaises(ValidationError):
            run_scout(lanes=["space_technology"], cache_dir=self.root, offline=True)

    def test_cli_command_is_wired_without_network(self) -> None:
        expected = {"ok": True, "candidate_count": 0, "ideas": []}
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("video_factory.cli.run_scout", return_value=expected) as mocked:
            code = main(
                [
                    "scout",
                    "--date",
                    "2026-08-27",
                    "--limit",
                    "28",
                    "--cache-dir",
                    str(self.root),
                    "--timeout",
                    "0.5",
                    "--retries",
                    "0",
                    "--offline",
                ],
                out=stdout,
                err=stderr,
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), expected)
        mocked.assert_called_once_with(
            production_date="2026-08-27",
            limit=28,
            cache_dir=str(self.root),
            timeout=0.5,
            retries=0,
            offline=True,
            lanes=None,
        )

    def test_cli_accepts_comma_separated_production_lane_filter(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "video_factory.cli.run_scout",
            return_value={"ok": True, "candidate_count": 0, "ideas": []},
        ) as mocked:
            code = main(
                [
                    "scout",
                    "--offline",
                    "--lanes",
                    "health, chinese_medicine",
                ],
                out=stdout,
                err=stderr,
            )
        self.assertEqual(code, 0)
        mocked.assert_called_once_with(
            production_date=None,
            limit=12,
            cache_dir=".video-factory-cache/scout",
            timeout=8.0,
            retries=1,
            offline=True,
            lanes=["health", "chinese_medicine"],
        )


if __name__ == "__main__":
    unittest.main()
