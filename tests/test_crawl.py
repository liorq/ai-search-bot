"""
The crawl, and the link graph built on it.
==========================================
"""

from __future__ import annotations

from seo_core.sources import crawl as c
from seo_core.sources import linkgraph as lg
from seo_core.sources.queries import CTRCurve, QueryRow


def fetcher(pages: dict[str, str], *, status: dict[str, int] | None = None,
            redirects: dict[str, str] | None = None):
    def fetch(url: str) -> tuple[int, str, str]:
        final = (redirects or {}).get(url, url)
        return (status or {}).get(url, 200), pages.get(final, ""), final
    return fetch


def run(pages, **kwargs) -> c.Crawl:
    return c.crawl("https://x.com/", fetcher(pages, **kwargs),
                   delay=0, sleep=lambda _: None,
                   **{k: v for k, v in kwargs.items() if k in {"max_pages"}})


# ═══════════════════════════════════════════════════════
#  URLs
# ═══════════════════════════════════════════════════════

class TestNormalise:
    def test_tracking_parameters_and_fragments_go(self):
        assert c.normalise("https://x.com/a?utm_source=fb&id=3#top") == \
            "https://x.com/a?id=3"

    def test_the_default_port_goes(self):
        assert c.normalise("https://x.com:443/a") == "https://x.com/a"

    def test_the_trailing_slash_stays_exactly_as_written(self):
        """On some servers /about and /about/ really are different pages."""
        assert c.normalise("https://x.com/about/") != c.normalise("https://x.com/about")

    def test_a_relative_href_resolves_against_the_page_it_is_on(self):
        assert c.normalise("../b", "https://x.com/dir/a") == "https://x.com/b"

    def test_www_is_the_same_site_and_a_subdomain_is_not(self):
        assert c.same_site("https://www.x.com/a", "https://x.com/")
        assert not c.same_site("https://blog.x.com/a", "https://x.com/")

    def test_assets_and_protocols_that_are_not_pages_are_skipped(self):
        for url in ("mailto:a@b.com", "tel:123", "/logo.png", "/f.pdf", "#top"):
            assert not c.is_crawlable(url)


# ═══════════════════════════════════════════════════════
#  robots.txt
# ═══════════════════════════════════════════════════════

class TestRobots:
    def test_our_own_group_wins_over_the_wildcard(self):
        robots = c.parse_robots(
            "User-agent: *\nDisallow: /\n\n"
            f"User-agent: {c.USER_AGENT}\nDisallow: /private\n")
        assert robots.allows("https://x.com/anything")
        assert not robots.allows("https://x.com/private/thing")

    def test_the_longest_matching_rule_wins(self):
        robots = c.parse_robots("User-agent: *\nDisallow: /wp-\nAllow: /wp-content/")
        assert not robots.allows("https://x.com/wp-admin/")
        assert robots.allows("https://x.com/wp-content/uploads/a.html")

    def test_a_crawl_delay_raises_ours_and_never_lowers_it(self):
        robots = c.parse_robots("User-agent: *\nCrawl-delay: 5")
        assert robots.crawl_delay == 5.0

    def test_a_disallowed_page_is_recorded_and_not_fetched(self):
        pages = {"https://x.com/": '<a href="/secret">s</a><a href="/ok">o</a>',
                 "https://x.com/secret": "x", "https://x.com/ok": "y"}
        result = c.crawl("https://x.com/", fetcher(pages),
                         robots_text="User-agent: *\nDisallow: /secret",
                         delay=0, sleep=lambda _: None)
        assert result.blocked == ["https://x.com/secret"]
        assert "https://x.com/secret" not in result.pages


# ═══════════════════════════════════════════════════════
#  The walk
# ═══════════════════════════════════════════════════════

class TestCrawl:
    def test_depth_is_the_order_of_the_walk(self):
        pages = {
            "https://x.com/": '<a href="/a">A</a>',
            "https://x.com/a": '<a href="/b">B</a>',
            "https://x.com/b": '<a href="/c">C</a>',
            "https://x.com/c": "",
        }
        result = run(pages)
        assert [result.pages[u].depth for u in
                ("https://x.com/", "https://x.com/a",
                 "https://x.com/b", "https://x.com/c")] == [0, 1, 2, 3]

    def test_a_page_reachable_two_ways_takes_the_shorter_one(self):
        pages = {
            "https://x.com/": '<a href="/a">A</a><a href="/deep">D</a>',
            "https://x.com/a": '<a href="/deep">D</a>',
            "https://x.com/deep": "",
        }
        assert run(pages).pages["https://x.com/deep"].depth == 1

    def test_the_page_cap_is_honoured_and_said_out_loud(self):
        pages = {f"https://x.com/p{i}": f'<a href="/p{i + 1}">next</a>'
                 for i in range(20)}
        pages["https://x.com/"] = '<a href="/p0">start</a>'
        result = c.crawl("https://x.com/", fetcher(pages), max_pages=5,
                         delay=0, sleep=lambda _: None)
        assert len(result.pages) == 5
        assert result.stopped_at_cap

    def test_an_external_link_is_seen_but_not_followed(self):
        pages = {"https://x.com/": '<a href="https://other.com/a">out</a>'}
        result = run(pages)
        assert len(result.pages) == 1
        assert result.pages["https://x.com/"].links[0].target.startswith("https://other")
        assert result.internal_links == []

    def test_a_redirect_is_recorded_rather_than_hidden(self):
        pages = {"https://x.com/": '<a href="/old">o</a>', "https://x.com/new": ""}
        result = c.crawl("https://x.com/",
                         fetcher(pages, redirects={"https://x.com/old":
                                                   "https://x.com/new"}),
                         delay=0, sleep=lambda _: None)
        assert result.pages["https://x.com/old"].redirected

    def test_a_fetch_that_raises_does_not_stop_the_crawl(self):
        def fetch(url):
            if url.endswith("/bad"):
                raise ConnectionError("boom")
            return 200, '<a href="/bad">b</a><a href="/good">g</a>', url
        result = c.crawl("https://x.com/", fetch, delay=0, sleep=lambda _: None)
        assert result.pages["https://x.com/bad"].status == 0
        assert "https://x.com/good" in result.pages


class TestLinks:
    def test_anchor_text_is_read_and_markup_inside_it_is_stripped(self):
        links = c.extract_links("https://x.com/",
                                '<a href="/a">Spring <b>repair</b></a>')
        assert links[0].anchor == "Spring repair"

    def test_an_image_link_falls_back_to_its_alt_text(self):
        links = c.extract_links(
            "https://x.com/", '<a href="/a"><img src="x.png" alt="Springs"></a>')
        assert links[0].anchor == "Springs"
        assert not links[0].is_empty

    def test_a_link_with_no_text_at_all_is_flagged_as_empty(self):
        links = c.extract_links("https://x.com/",
                                '<a href="/a"><img src="x.png"></a>')
        assert links[0].is_empty

    def test_nofollow_is_read_off_the_rel_attribute(self):
        links = c.extract_links("https://x.com/",
                                '<a href="/a" rel="nofollow ugc">a</a>')
        assert links[0].nofollow

    def test_generic_anchors_are_recognised_in_both_languages(self):
        for text in ("Click here", "read more", "לחץ כאן", "קרא עוד"):
            assert c.Link("s", "t", text).is_generic
        assert not c.Link("s", "t", "torsion spring repair").is_generic


class TestBoilerplate:
    def _site(self, count: int) -> c.Crawl:
        body = '<a href="/contact">Contact</a><a href="/services">Services</a>'
        pages = {f"https://x.com/p{i}": body + f'<a href="/p{i+1}">next</a>'
                 for i in range(count)}
        pages["https://x.com/"] = body + '<a href="/p0">start</a>'
        pages["https://x.com/contact"] = body
        pages["https://x.com/services"] = body
        return c.crawl("https://x.com/", fetcher(pages), delay=0,
                       sleep=lambda _: None)

    def test_a_link_on_every_page_is_navigation_not_a_recommendation(self):
        crawled = self._site(20)
        keys = crawled.boilerplate_keys()
        assert ("https://x.com/contact", "contact") in keys
        editorial = {l.target for l in crawled.editorial_links()}
        assert "https://x.com/contact" not in editorial

    def test_on_a_small_site_nothing_is_called_navigation(self):
        """Half of a six-page site linking to /contact is ordinary."""
        assert self._site(3).boilerplate_keys() == set()


# ═══════════════════════════════════════════════════════
#  The graph
# ═══════════════════════════════════════════════════════

def curve() -> CTRCurve:
    return CTRCurve(
        buckets={1: 0.28, 2: 0.16, 3: 0.11, 4: 0.08, 5: 0.06, 6: 0.05,
                 7: 0.04, 8: 0.032, 10: 0.025, 12: 0.018, 20: 0.008},
        source="site", sample=640,
    )


def wide_site(*, nav='<a href="/contact">Contact</a>', extra="") -> c.Crawl:
    """Twenty pages carrying a menu, so boilerplate detection has something
    to work with, plus one page nobody links to editorially."""
    pages = {}
    for i in range(20):
        body = nav + f'<a href="/p{i + 1}">next</a>'
        if i == 0:
            body += extra
        pages[f"https://x.com/p{i}"] = body
    pages["https://x.com/"] = nav + '<a href="/p0">start</a>'
    pages["https://x.com/contact"] = nav
    return c.crawl("https://x.com/", fetcher(pages), delay=0, sleep=lambda _: None)


class TestGraph:
    def test_equity_is_conserved_even_with_dead_end_pages(self):
        graph = lg.build(wide_site())
        total = sum(n.pagerank for n in graph.nodes.values())
        assert abs(total - 1.0) < 0.01

    def test_a_page_linked_from_everywhere_ranks_above_one_linked_once(self):
        graph = lg.build(wide_site())
        contact = graph.get("https://x.com/contact")
        deep = graph.get("https://x.com/p15")
        assert contact.pagerank > deep.pagerank

    def test_a_menu_link_counts_for_pagerank_and_not_as_an_endorsement(self):
        """A crawler sees the menu; a human recommending a page does not."""
        graph = lg.build(wide_site())
        contact = graph.get("https://x.com/contact")
        assert len(contact.inbound) > 10
        assert contact.editorial_inbound == []
        assert contact in graph.unsupported()

    def test_a_nofollow_link_is_not_an_endorsement(self):
        crawled = wide_site(extra='<a href="/p5" rel="nofollow">springs</a>')
        graph = lg.build(crawled)
        anchors = [l.anchor for l in graph.get("https://x.com/p5").editorial_inbound]
        assert "springs" not in anchors

    def test_a_self_link_is_ignored(self):
        pages = {"https://x.com/": '<a href="/">home</a><a href="/a">a</a>',
                 "https://x.com/a": ""}
        graph = lg.build(c.crawl("https://x.com/", fetcher(pages), delay=0,
                                 sleep=lambda _: None))
        assert graph.get("https://x.com/").inbound == []


class TestOrphans:
    def test_a_crawl_alone_cannot_find_an_orphan(self):
        """The page has no inbound link, so nothing leads the crawl to it."""
        pages = {"https://x.com/": '<a href="/a">a</a>', "https://x.com/a": ""}
        graph = lg.build(c.crawl("https://x.com/", fetcher(pages), delay=0,
                                 sleep=lambda _: None))
        assert "https://x.com/orphan" not in graph.nodes

    def test_an_orphan_is_found_by_comparing_against_a_known_list(self):
        pages = {"https://x.com/": '<a href="/a">a</a>', "https://x.com/a": ""}
        graph = lg.build(c.crawl("https://x.com/", fetcher(pages), delay=0,
                                 sleep=lambda _: None))
        assert graph.orphans(["https://x.com/a", "https://x.com/orphan?utm_source=x"]) \
            == ["https://x.com/orphan"]

    def test_the_front_door_is_never_an_orphan(self):
        pages = {"https://x.com/": ""}
        graph = lg.build(c.crawl("https://x.com/", fetcher(pages), delay=0,
                                 sleep=lambda _: None))
        assert graph.orphans(["https://x.com/"]) == []


class TestAnchorProblems:
    def test_a_page_mostly_linked_as_click_here_is_flagged(self):
        crawled = wide_site(extra='<a href="/p7">click here</a>')
        graph = lg.build(crawled)
        kinds = {p.kind for p in lg.anchor_problems(graph) if p.url.endswith("/p7")}
        assert "generic" in kinds

    def test_a_descriptive_anchor_is_not_a_problem(self):
        crawled = wide_site(extra='<a href="/p7">torsion spring repair</a>')
        graph = lg.build(crawled)
        assert not [p for p in lg.anchor_problems(graph) if p.url.endswith("/p7")]

    def test_an_anchor_that_misses_the_pages_own_terms_is_flagged(self):
        crawled = wide_site(extra='<a href="/p7">our winter offer</a>')
        graph = lg.build(crawled)
        problems = lg.anchor_problems(
            graph, page_terms={"https://x.com/p7": {"torsion", "spring"}})
        assert any(p.kind == "off_topic" and p.url.endswith("/p7") for p in problems)


class TestSupportGaps:
    def rows(self, url: str, impressions: int, clicks: int, position: float):
        return [QueryRow(query="torsion spring repair", url=url, clicks=clicks,
                         impressions=impressions, position=position)]

    def test_demand_without_support_is_the_finding(self):
        graph = lg.build(wide_site())
        gaps = lg.find_support_gaps(
            graph, self.rows("https://x.com/p15", 4000, 40, 8.0), curve())
        assert [g.url for g in gaps] == ["https://x.com/p15"]

    def test_a_page_nobody_searches_for_is_not_an_opportunity(self):
        """Being top of a demand set of one does not make 60 impressions demand."""
        graph = lg.build(wide_site())
        assert lg.find_support_gaps(
            graph, self.rows("https://x.com/p15", 60, 0, 8.0), curve()) == []

    def test_a_gain_of_a_few_clicks_does_not_earn_a_line_in_the_queue(self):
        graph = lg.build(wide_site())
        rows = self.rows("https://x.com/p15", 400, 22, 5.0)
        gaps = lg.find_support_gaps(graph, rows, curve())
        assert all(g.potential_clicks >= lg.MIN_CLAIMABLE_CLICKS for g in gaps)

    def test_a_page_already_at_the_top_is_left_alone(self):
        graph = lg.build(wide_site())
        assert lg.find_support_gaps(
            graph, self.rows("https://x.com/p15", 4000, 900, 2.0), curve()) == []

    def test_a_page_on_the_fourth_page_of_results_is_not_a_link_problem(self):
        graph = lg.build(wide_site())
        assert lg.find_support_gaps(
            graph, self.rows("https://x.com/p15", 4000, 2, 34.0), curve()) == []

    def test_a_site_too_small_for_quantiles_produces_nothing(self):
        pages = {"https://x.com/": '<a href="/a">a</a>', "https://x.com/a": ""}
        graph = lg.build(c.crawl("https://x.com/", fetcher(pages), delay=0,
                                 sleep=lambda _: None))
        assert lg.find_support_gaps(
            graph, self.rows("https://x.com/a", 9000, 10, 8.0), curve()) == []


class TestProposals:
    def crawled(self) -> c.Crawl:
        pages = {
            "https://x.com/": '<a href="/a">a</a><a href="/b">b</a>'
                              '<a href="/target">t</a>',
            "https://x.com/a": "<p>We replace every torsion spring in Denver.</p>",
            "https://x.com/b": '<p>Torsion spring work</p>'
                               '<a href="/target">already linked</a>',
            "https://x.com/target": "<p>Torsion spring repair page</p>",
        }
        return c.crawl("https://x.com/", fetcher(pages), delay=0,
                       sleep=lambda _: None)

    def test_a_page_that_already_says_the_phrase_is_the_candidate(self):
        crawled = self.crawled()
        proposals = lg.propose(crawled, lg.build(crawled),
                               "https://x.com/target", ["torsion spring"])
        assert [p.source for p in proposals] == ["https://x.com/a"]
        assert proposals[0].phrase == "torsion spring"

    def test_a_page_that_already_links_is_not_proposed_again(self):
        crawled = self.crawled()
        proposals = lg.propose(crawled, lg.build(crawled),
                               "https://x.com/target", ["torsion spring"])
        assert "https://x.com/b" not in [p.source for p in proposals]

    def test_the_target_never_proposes_a_link_to_itself(self):
        crawled = self.crawled()
        proposals = lg.propose(crawled, lg.build(crawled),
                               "https://x.com/target", ["torsion spring"])
        assert "https://x.com/target" not in [p.source for p in proposals]

    def test_anchor_candidates_come_from_what_the_page_ranks_for(self):
        rows = [QueryRow("torsion spring repair", "https://x.com/t", 10, 900, 8.0),
                QueryRow("springs", "https://x.com/t", 5, 400, 9.0),
                QueryRow("cable repair cost", "https://x.com/other", 5, 400, 9.0)]
        assert lg.phrases_for(rows, "https://x.com/t") == ["torsion spring repair"]


class TestGapFinding:
    def test_a_gap_with_no_source_page_says_the_link_must_be_written(self):
        graph = lg.build(wide_site())
        gap = lg.find_support_gaps(
            graph,
            [QueryRow("torsion spring repair", "https://x.com/p15", 40, 4000, 8.0)],
            curve())[0]
        finding = lg.gap_to_finding(gap, "x.com", curve(), [])
        assert finding.confidence == "low"
        assert "ידנית" in finding.confidence_reason
        assert finding.action["kind"] == "add_internal_links"
