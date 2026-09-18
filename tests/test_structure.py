"""
Site structure — what the crawl proves, and what it only suggests.
=================================================================
"""

from __future__ import annotations

from seo_core.sources import crawl as c
from seo_core.sources import structure as st
from seo_core.sources.queries import CTRCurve, QueryRow


def curve() -> CTRCurve:
    return CTRCurve(
        buckets={1: 0.28, 2: 0.16, 3: 0.11, 4: 0.08, 5: 0.06, 6: 0.05,
                 7: 0.04, 8: 0.032, 10: 0.025, 12: 0.018, 20: 0.008},
        source="site", sample=640,
    )


def build(pages, *, status=None, redirects=None) -> c.Crawl:
    def fetch(url: str) -> tuple[int, str, str]:
        final = (redirects or {}).get(url, url)
        return (status or {}).get(url, 200), pages.get(final, ""), final
    return c.crawl("https://x.com/", fetch, delay=0, sleep=lambda _: None)


def row(url, impressions=900, clicks=10, position=8.0, query="torsion spring"):
    return QueryRow(query=query, url=url, clicks=clicks,
                    impressions=impressions, position=position)


# ═══════════════════════════════════════════════════════
#  Facts
# ═══════════════════════════════════════════════════════

class TestBroken:
    def test_a_link_to_a_404_is_reported_with_who_links_to_it(self):
        pages = {"https://x.com/": '<a href="/gone">Old page</a><a href="/a">a</a>',
                 "https://x.com/a": '<a href="/gone">Old page</a>'}
        report = st.broken_links(build(pages, status={"https://x.com/gone": 404}))

        assert report[0].url == "https://x.com/gone"
        assert report[0].linked_from == ["https://x.com/", "https://x.com/a"]
        assert report[0].anchors == ["Old page"]

    def test_a_page_that_never_answered_is_distinguished_from_a_404(self):
        def fetch(url):
            if url.endswith("/dead"):
                raise TimeoutError("no answer")
            return 200, '<a href="/dead">d</a>', url
        crawled = c.crawl("https://x.com/", fetch, delay=0, sleep=lambda _: None)
        assert "לא נענה בכלל" in st.broken_links(crawled)[0].describe()

    def test_the_most_linked_broken_target_comes_first(self):
        pages = {"https://x.com/": '<a href="/one">1</a><a href="/two">2</a>'
                                   '<a href="/a">a</a>',
                 "https://x.com/a": '<a href="/two">2</a>'}
        report = st.broken_links(build(pages, status={"https://x.com/one": 404,
                                                      "https://x.com/two": 404}))
        assert report[0].url == "https://x.com/two"


class TestRedirects:
    def test_a_single_hop_is_recorded_but_is_not_a_finding(self):
        pages = {"https://x.com/": '<a href="/old">o</a>', "https://x.com/new": ""}
        crawled = build(pages, redirects={"https://x.com/old": "https://x.com/new"})
        found = st.redirected_links(crawled)

        assert found[0].url == "https://x.com/old"
        assert not found[0].is_chain
        report = st.Report(redirected=found)
        assert not [f for f in st.to_findings(report, "x.com", curve())
                    if f.type == "redirect_chain"]

    def test_a_chain_of_two_hops_earns_its_own_finding(self):
        pages = {"https://x.com/": '<a href="/old">o</a>', "https://x.com/new": ""}
        crawled = build(pages, redirects={"https://x.com/old": "https://x.com/new"})
        traces = {"https://x.com/old": c.Trace(
            chain=["https://x.com/old", "https://x.com/mid", "https://x.com/new"])}

        found = st.redirected_links(crawled, traces)
        assert found[0].is_chain
        findings = st.to_findings(st.Report(redirected=found), "x.com", curve())
        assert findings[0].evidence["hops"] == 2

    def test_a_loop_is_always_a_chain(self):
        pages = {"https://x.com/": '<a href="/old">o</a>', "https://x.com/new": ""}
        crawled = build(pages, redirects={"https://x.com/old": "https://x.com/new"})
        traces = {"https://x.com/old": c.Trace(
            chain=["https://x.com/old", "https://x.com/old"], looped=True)}
        assert st.redirected_links(crawled, traces)[0].is_chain


class TestBlocked:
    def test_a_noindex_page_that_still_earns_impressions_is_raised(self):
        pages = {"https://x.com/": '<a href="/staging">s</a>',
                 "https://x.com/staging": '<meta name="robots" content="noindex">'}
        found = st.blocked_but_earning(build(pages), [row("https://x.com/staging")])

        assert found[0].reason == "noindex"
        assert "noindex" in found[0].describe()

    def test_a_robots_blocked_page_with_demand_is_raised(self):
        pages = {"https://x.com/": '<a href="/private">p</a>'}
        crawled = c.crawl("https://x.com/", lambda u: (200, pages.get(u, ""), u),
                          robots_text="User-agent: *\nDisallow: /private",
                          delay=0, sleep=lambda _: None)
        found = st.blocked_but_earning(crawled, [row("https://x.com/private")])
        assert found[0].reason == "robots"

    def test_a_blocked_page_nobody_searches_for_is_not_raised(self):
        pages = {"https://x.com/": '<a href="/staging">s</a>',
                 "https://x.com/staging": '<meta name="robots" content="noindex">'}
        assert st.blocked_but_earning(build(pages), []) == []

    def test_the_finding_proposes_a_review_and_never_a_change(self):
        pages = {"https://x.com/": '<a href="/staging">s</a>',
                 "https://x.com/staging": '<meta name="robots" content="noindex">'}
        report = st.Report(blocked=st.blocked_but_earning(
            build(pages), [row("https://x.com/staging")]))
        finding = st.to_findings(report, "x.com", curve())[0]
        assert finding.action["kind"] == "review_indexability"


# ═══════════════════════════════════════════════════════
#  The signal
# ═══════════════════════════════════════════════════════

class TestDepth:
    def chain_site(self) -> c.Crawl:
        pages = {"https://x.com/": '<a href="/p1">1</a>'}
        for i in range(1, 7):
            pages[f"https://x.com/p{i}"] = f'<a href="/p{i + 1}">next</a>'
        pages["https://x.com/p7"] = ""
        return build(pages)

    def test_depth_alone_is_not_a_finding(self):
        """A page nobody searches for does not get better by moving up."""
        assert st.deep_pages(self.chain_site(), [], curve()) == []

    def test_depth_with_demand_is_raised(self):
        found = st.deep_pages(
            self.chain_site(), [row("https://x.com/p6", impressions=3000)], curve())
        assert found[0].url == "https://x.com/p6"
        assert found[0].depth == 6

    def test_a_shallow_page_is_never_raised_however_much_demand_it_has(self):
        assert st.deep_pages(
            self.chain_site(), [row("https://x.com/p2", impressions=9000)],
            curve()) == []

    def test_the_finding_names_a_shallower_page_to_link_from(self):
        found = st.deep_pages(
            self.chain_site(), [row("https://x.com/p6", impressions=3000)], curve())
        assert found[0].path_hint == "https://x.com/p5"

    def test_depth_claims_less_than_an_on_page_rewrite_would(self):
        found = st.deep_pages(
            self.chain_site(), [row("https://x.com/p6", impressions=3000)], curve())
        gap = 3000 * curve().expected(6.5) - 10
        assert found[0].potential_clicks < gap

    def test_a_noindexed_deep_page_is_not_a_depth_problem(self):
        pages = {"https://x.com/": '<a href="/p1">1</a>'}
        for i in range(1, 6):
            pages[f"https://x.com/p{i}"] = f'<a href="/p{i + 1}">next</a>'
        pages["https://x.com/p5"] = '<meta name="robots" content="noindex">'
        assert st.deep_pages(
            build(pages), [row("https://x.com/p5", impressions=3000)], curve()) == []


class TestReport:
    def test_facts_are_ranked_above_the_signal(self):
        pages = {"https://x.com/": '<a href="/gone">g</a><a href="/p1">1</a>'}
        for i in range(1, 7):
            pages[f"https://x.com/p{i}"] = f'<a href="/p{i + 1}">next</a>'
        crawled = build(pages, status={"https://x.com/gone": 404})

        rows = [row("https://x.com/p6", impressions=3000)]
        report = st.analyse(crawled, rows, curve())
        findings = st.to_findings(report, "x.com", curve())

        kinds = [f.type for f in findings]
        assert kinds.index("broken_internal_link") < kinds.index("deep_page_with_demand")

    def test_a_broken_link_claims_no_clicks_because_it_is_a_fault(self):
        pages = {"https://x.com/": '<a href="/gone">g</a>'}
        report = st.analyse(build(pages, status={"https://x.com/gone": 404}),
                            [], curve())
        finding = st.to_findings(report, "x.com", curve())[0]
        assert finding.impact_clicks == 0.0
        assert finding.confidence == "high"

    def test_wasted_links_counts_both_broken_and_redirected(self):
        pages = {"https://x.com/": '<a href="/gone">g</a><a href="/old">o</a>',
                 "https://x.com/new": ""}
        crawled = build(pages, status={"https://x.com/gone": 404},
                        redirects={"https://x.com/old": "https://x.com/new"})
        assert st.analyse(crawled, [], curve()).wasted_links == 2


class TestTrace:
    def test_a_chain_is_counted_hop_by_hop(self):
        hops = {"https://x.com/a": "https://x.com/b", "https://x.com/b": "https://x.com/c"}
        def fetch(url):
            return (301, "", hops[url]) if url in hops else (200, "", url)
        result = c.trace("https://x.com/a", fetch)
        assert result.hops == 2
        assert result.destination == "https://x.com/c"

    def test_a_loop_is_detected_rather_than_followed_forever(self):
        loop = {"https://x.com/a": "https://x.com/b", "https://x.com/b": "https://x.com/a"}
        def fetch(url):
            return 301, "", loop[url]
        result = c.trace("https://x.com/a", fetch)
        assert result.looped
        assert "לולאת" in result.describe()

    def test_an_endless_chain_stops_at_the_hop_limit(self):
        def fetch(url):
            return 301, "", url + "x"
        result = c.trace("https://x.com/a", fetch, max_hops=4)
        assert result.truncated
