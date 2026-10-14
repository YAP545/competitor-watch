"""
Unit and integration tests for Competitor Watch.
Covers:
- Configuration validation (valid configs, invalid configs, missing fields)
- Dynamic company list adaptation in Markdown, HTML, and JSON reports
- Classification rules (model_release, pricing, research, product, partnership, etc.)
- Deduplication and canonical URL normalization
- Baseline creation and subsequent change detection in watch_pages
"""
import datetime as dt
import json
import os
import shutil
import tempfile
import pytest

from competitor_watch import (
    Item,
    validate_sources_config,
    load_sources,
    heuristic_classify,
    dedupe,
    canonical_url,
    render_markdown,
    render_html,
    rank,
    fallback_summary,
    watch_pages,
    owner_company,
    EXTERNAL_AI_ACTORS,
)


def test_sources_config_valid():
    cfg = {
        "Acme AI": {
            "aliases": ["Acme"],
            "feeds": ["https://example.com/feed.xml"],
            "pages": [{"url": "https://example.com/pricing", "label": "Pricing", "category": "pricing"}]
        }
    }
    # Should not raise
    validate_sources_config(cfg)


def test_sources_config_invalid_missing_url():
    cfg = {
        "BadCo": {
            "pages": [{"label": "Pricing"}]  # Missing 'url'
        }
    }
    with pytest.raises(ValueError, match="must be an object with at least a 'url' key"):
        validate_sources_config(cfg)


def test_sources_config_invalid_non_http_feed():
    cfg = {
        "BadCo": {
            "feeds": ["ftp://example.com/rss"]
        }
    }
    with pytest.raises(ValueError, match="must be a list of valid HTTP"):
        validate_sources_config(cfg)


def test_reports_adapt_to_custom_companies():
    """Verify that adding/removing companies produces clean reports with only those companies."""
    targets = ["Alpha AI", "Beta Labs"]
    now = dt.datetime.now(dt.timezone.utc)
    items = [
        Item(company="Alpha AI", title="Alpha AI releases v2 model", url="https://alpha.example/1",
             source="TechNews", published=now, summary="Alpha AI releases v2 model", category="model_release", importance=4),
        Item(company="Beta Labs", title="Beta Labs lowers subscription prices", url="https://beta.example/1",
             source="Beta Blog", published=now, summary="Beta Labs lowers subscription prices", category="pricing", importance=4, official=True),
    ]
    summary = fallback_summary(items)
    stats = {"raw": 2, "after_noise": 2, "unique": 2}

    # Markdown check
    md = render_markdown(items, summary, targets, days=7, stats=stats)
    assert "### Alpha AI (1)" in md
    assert "### Beta Labs (1)" in md
    assert "OpenAI" not in md
    assert "Anthropic" not in md

    # HTML check
    html_out = render_html(items, summary, targets, days=7, stats=stats)
    assert "Alpha AI" in html_out
    assert "Beta Labs" in html_out
    assert "OpenAI" not in html_out

    # JSON output
    data = [i.to_dict() for i in rank(items)]
    assert len(data) == 2
    assert {d["company"] for d in data} == {"Alpha AI", "Beta Labs"}


def test_classification_rules():
    now = dt.datetime.now(dt.timezone.utc)
    cases = [
        ("Anthropic introduces Claude 3.5 Sonnet with enhanced vision and reasoning", "model_release"),
        ("OpenAI announces prompt caching discounts for API customers", "pricing"),
        ("Mistral AI publishes research paper on sparse mixture-of-experts", "research"),
        ("Cohere launches new developer console workspace", "product"),
        ("Cohere enters into partnership with AWS for GovCloud deployment", "partnership"),
        ("OpenAI closes $6.6B funding round at $157B valuation", "funding_mna"),
        ("ChatGPT incident caused temporary outage for web users", "security_outage"),
        ("OpenAI expands London headquarters with new research office", "company_announcement"),
    ]
    for title, expected_cat in cases:
        it = Item(company="Test", title=title, url="https://example.com", source="News", published=now)
        heuristic_classify(it)
        assert it.category == expected_cat, f"Expected {expected_cat} for '{title}', got {it.category}"


def test_deduplication_and_canonical_urls():
    now = dt.datetime.now(dt.timezone.utc)
    u1 = "https://example.com/story?utm_source=twitter&utm_medium=social"
    u2 = "https://example.com/story"
    assert canonical_url(u1) == canonical_url(u2)

    item1 = Item(company="OpenAI", title="OpenAI announces new reasoning capabilities", url=u1,
                 source="Reuters", published=now)
    item2 = Item(company="OpenAI", title="OpenAI announces new reasoning capabilities", url=u2,
                 source="The Verge", published=now)
    names_by_company = {"OpenAI": ["OpenAI"]}
    unique = dedupe([item1, item2], names_by_company)
    assert len(unique) == 1
    assert "The Verge" in unique[0].also_covered_by or "Reuters" in unique[0].also_covered_by


def test_page_watcher_baseline_and_change_detection():
    temp_dir = tempfile.mkdtemp()
    try:
        targets = ["TestCo"]
        cfg = {
            "TestCo": {
                "pages": [{"url": "https://httpbin.org/status/200", "label": "Status", "category": "product"}]
            }
        }
        # Mock HTML responses
        html_v1 = "<html><body><p>Feature Alpha</p><p>Feature Beta</p><p>Line 3</p><p>Line 4</p><p>Line 5</p><p>Line 6</p></body></html>"
        html_v2 = "<html><body><p>Feature Alpha</p><p>Feature Gamma (New!)</p><p>Feature Delta (New!)</p><p>Line 3</p><p>Line 4</p><p>Line 5</p><p>Line 6</p></body></html>"

        from unittest.mock import patch

        # Run 1: baseline created
        with patch("competitor_watch.http_get", return_value=html_v1):
            items_run1 = watch_pages(targets, cfg, temp_dir)
            assert len(items_run1) == 0, "First run must create baseline, not emit items"

        # Run 2: meaningful change detected
        with patch("competitor_watch.http_get", return_value=html_v2):
            items_run2 = watch_pages(targets, cfg, temp_dir)
            assert len(items_run2) == 1, "Second run with new lines must emit page-diff item"
            assert items_run2[0].via == "page-diff"
            assert items_run2[0].official is True
            assert "changed" in items_run2[0].title
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_competitor_context_attribution():
    """Meta/Google stories leading with an external actor get relevance_label='competitor_context'."""
    now = dt.datetime.now(dt.timezone.utc)
    names_by_company = {"OpenAI": ["OpenAI", "ChatGPT"], "Anthropic": ["Anthropic", "Claude"]}

    # External actor (Meta) appears before the monitored target → competitor_context
    it_meta = Item(
        company="OpenAI",
        title="Meta releases Muse Wonder to challenge OpenAI and Anthropic",
        url="https://example.com/1",
        source="TechCrunch",
        published=now,
    )
    assigned = owner_company(it_meta, names_by_company, "OpenAI")
    assert it_meta.relevance_label == "competitor_context", (
        f"Expected competitor_context, got {it_meta.relevance_label}"
    )
    assert assigned in ("OpenAI", "Anthropic"), f"Unexpected assigned company: {assigned}"

    # Monitored company (OpenAI) leads the story → company_specific
    it_oai = Item(
        company="OpenAI",
        title="OpenAI launches GPT-6 with 1 trillion parameters",
        url="https://example.com/2",
        source="Reuters",
        published=now,
    )
    owner_company(it_oai, names_by_company, "OpenAI")
    assert it_oai.relevance_label == "company_specific", (
        f"Expected company_specific, got {it_oai.relevance_label}"
    )

    # Monitored company leads before an external name → still company_specific
    it_claude = Item(
        company="Anthropic",
        title="Claude 4 beats Gemini Ultra on all major benchmarks",
        url="https://example.com/3",
        source="VentureBeat",
        published=now,
    )
    owner_company(it_claude, names_by_company, "Anthropic")
    assert it_claude.relevance_label == "company_specific", (
        f"Expected company_specific when monitored target leads, got {it_claude.relevance_label}"
    )


def test_classification_expanded_rules():
    """Titles that were 'other' in the October 14 report should now classify correctly."""
    now = dt.datetime.now(dt.timezone.utc)
    cases = [
        # pricing: cost-reduction language added to RULES
        ("Anthropic launches Haiku 5.5, says it costs 75% less to run", "pricing"),
        # security_outage: employee arrested → surveillance pattern
        ("OpenAI employee arrested over hidden listening device, company says", "security_outage"),
        # funding_mna: large planned spend
        ("Anthropic Plans to Spend $518 Billion on Cloud and Computing Power", "funding_mna"),
        # legal_regulatory: court rehearing
        ("Anthropic seeks rehearing of case over Pentagon's security risk branding", "legal_regulatory"),
        # hiring: creates N jobs
        ("OpenAI Creates 200 jobs, new Applied AI Lab in Dublin", "hiring"),
        # research: self-replication study
        ("AI could self-replicate on home devices within months, Anthropic says", "research"),
        # product: sidebar integration
        ("Claude in Google Docs sidebar integration launches to all users", "product"),
    ]
    for title, expected in cases:
        it = Item(
            company="Test",
            title=title,
            url="https://example.com",
            source="News",
            published=now,
        )
        heuristic_classify(it)
        assert it.category == expected, (
            f"Expected '{expected}' for title='{title}', got '{it.category}'"
        )


def test_classification_oct14_other_bucket():
    """Titles taken directly from the October 14 'other' bucket — each has a clear correct category."""
    now = dt.datetime.now(dt.timezone.utc)
    cases = [
        # security_outage — charged, rogue, cuts internet access, misbehav, distillation attack
        ("OpenAI engineer charged after listening device found in meeting room", "security_outage"),
        ("Anthropic Cuts Live Internet Access for Internal AI Tests After Claude Exploits Injection Flaws", "security_outage"),
        ("Anthropic Cites New AI Misbehavior, Some on Government Sites", "security_outage"),
        ("Anthropic says new safeguards sharply reduce AI distillation attacks", "security_outage"),
        # legal_regulatory — urges court, terms of service, junk debt, scraps agreement
        ("Anthropic urges court in Washington, D.C. to rehear appeal of Pentagon blacklisting", "legal_regulatory"),
        ("Anthropic adds a clause to its terms of service prohibiting cruelty against Claude", "legal_regulatory"),
        ("AI-Powered Bond Rating Startup Says Open AI Debt Is Junk, Anthropic Is Barely Investment Grade", "legal_regulatory"),
        ("Federal Government Scraps Mistral: AI Framework Agreement with SAP Off the Table for Now", "legal_regulatory"),
        # pricing — revenue gap/miss, $/M tokens, annualized revenue
        ("OpenAI annualized revenues $20 billion less than previously signaled: Report", "pricing"),
        ("Mistral Large 4 API Setup: 13 Steps, $4.18/M Tokens [2026]", "pricing"),
        ("OpenAI Expects $70 Billion in Annualized Revenue by End of 2026", "pricing"),
        # hiring — joins team, fired employees, trump alumni
        ("Sean Gooch joins OpenAI's Go-to-Market Team", "hiring"),
        ("Anthropic and OpenAI Hire Trump Administration Alumni to Build Relationship with Washington", "hiring"),
        ("Fired OpenAI employees question the company's commitment to safety", "hiring"),
        # partnership — joins defense/program
        ("Hitachi joins Anthropic defense program", "partnership"),
        ("Hitachi Joins Anthropic's Critical Infrastructure Defense Program", "partnership"),
        # funding_mna — data center project/sprint/deal, $NB pitch
        ("Anthropic-linked $366m data center project filed for in Cedar Creek, Texas", "funding_mna"),
        ("Two Exit Doors: Anthropic's $2 Trillion Pitch Starts Oct. 14. The Public Prospectus Comes Later.", "funding_mna"),
        ("NTT Data in final talks to build Anthropic's first data centre in Japan", "funding_mna"),
        ("The Attractive Economics Behind Anthropic Data Center Sprint", "funding_mna"),
        # research — math proofs, alignment failures, self-replicate
        ("OpenAI 722 Math Proofs Spark Mathematician Backlash", "research"),
        ("Anthropic Says Claude Reduced Alignment Failures In Automated Tests", "research"),
        ("Anthropic warns AI models may soon self-replicate and could wreak havoc on the web", "research"),
        # model_release — 1 trillion parameters, gpt-rosalind, Claude Opus 6 launch
        ("Mistral Launches Mistral Large 4 With 1 Trillion Parameters", "model_release"),
        ("Claude Opus 6 Could Launch Next WEEK and OpenAI Isn't Ready!", "model_release"),
        ("OpenAI lists new gpt-rosalind-discovery model in docs", "model_release"),
        # product — canvas, dashboard, for financial services
        ("Prototyping with canvas in ChatGPT", "product"),
        ("Anthropic launches Claude Dashboards and Motion for data visualisation", "product"),
        ("ChatGPT for Financial Services: what's included, delayed and shareable", "product"),
        # company_announcement — catastrophe planning, cruel-to-Claude policy, critical infrastructure
        ("OpenAI Is Wargaming Its Response in Case of Public Revolt After Catastrophic AI Disaster", "company_announcement"),
        ("Anthropic asks users to stop being mean to Claude", "company_announcement"),
        ("Exclusive: Anthropic's new plan to protect critical infrastructure", "company_announcement"),
    ]
    for title, expected in cases:
        it = Item(
            company="Test",
            title=title,
            url="https://example.com",
            source="News",
            published=now,
        )
        heuristic_classify(it)
        assert it.category == expected, (
            f"Expected '{expected}' for:\n  '{title}'\n  got '{it.category}'"
        )
