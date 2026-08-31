"""Checks for the build-free production website."""

import json
import re
from pathlib import Path

from lummenna.bench_results import load_evaluation_summary

ROOT = Path(__file__).resolve().parent.parent
WEBSITE = ROOT / "website"
PAGES = ["index.html", "features.html", "pricing.html", "docs.html", "404.html", "thanks.html"]

#: Pages carrying the full site chrome. 404 and thanks are deliberate
#: dead ends with a single escape link and no navigation to skip past.
CHROME_PAGES = ["index.html", "features.html", "pricing.html", "docs.html"]

#: Every stylesheet a member of the public can load. The marketing site ships to
#: Azure Static Web Apps while the observatory and the API reference are served
#: by the application itself, so they cannot share a file at runtime; keeping
#: them in step is therefore a test obligation rather than an import.
PUBLIC_STYLESHEETS = [
    WEBSITE / "assets" / "css" / "site.css",
    ROOT / "lummenna" / "api" / "dashboard.html",
    ROOT / "lummenna" / "api" / "api_docs.html",
]

#: Every HTML document a member of the public can open, marketing and app alike.
PUBLIC_HTML = [WEBSITE / page for page in PAGES] + [
    ROOT / "lummenna" / "api" / "dashboard.html",
    ROOT / "lummenna" / "api" / "api_docs.html",
]

#: The one brand palette. A surface that drifts from this stops matching the
#: others, which is exactly the drift duplication invites.
THEME_TOKENS = {
    "--paper": "#eee9dd",
    "--paper-bright": "#f8f5ec",
    "--paper-deep": "#ded7c7",
    "--ink": "#101b19",
    "--ink-soft": "#344440",
    "--forest": "#173b33",
    "--forest-deep": "#0d2722",
    "--jade": "#0d745b",
    "--jade-bright": "#28a982",
    "--amber": "#bd6d19",
    "--amber-ink": "#84470d",
    "--amber-bright": "#e8a84a",
    "--rust": "#a44431",
    "--line": "#b9b1a1",
    "--line-dark": "#355149",
    "--white": "#fffdf7",
    "--display": 'Georgia, "Times New Roman", serif',
    "--sans": '"Avenir Next", Avenir, "Helvetica Neue", Helvetica, Arial, sans-serif',
    "--mono": 'SFMono-Regular, "SF Mono", Menlo, Monaco, Consolas, "Liberation Mono", monospace',
}

#: ``font:`` shorthand whose family arrives through ``var()``. The shorthand
#: also resets font-weight, font-style, font-variant and line-height to their
#: initial values, so a single declaration silently controls six properties.
#: Longhands state the intent and cannot lose a property to a parse failure.
FONT_SHORTHAND_WITH_VAR = re.compile(r"font:\s*[^;{}]*var\(--")

#: CSS block comments, stripped before scanning so prose that names a banned
#: construct in order to explain it is not mistaken for a use of it.
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def stylesheet_source(path: Path) -> str:
    """Return a stylesheet's declarations with comments removed."""
    return CSS_COMMENT.sub("", path.read_text(encoding="utf-8"))


def test_all_pages_use_the_observatory_theme_and_local_favicon():
    for page_name in PAGES:
        page = (WEBSITE / page_name).read_text(encoding="utf-8")
        assert "/assets/css/site.css" in page
        assert "/assets/img/favicon.svg" in page
        assert "https://cdn." not in page
        assert "style=" not in page


def test_skip_links_target_programmatically_focusable_main_content():
    for page_name in CHROME_PAGES:
        page = (WEBSITE / page_name).read_text(encoding="utf-8")
        assert 'href="#main-content"' in page
        assert '<main id="main-content" tabindex="-1">' in page


def test_previous_brand_artwork_is_used_by_current_pages():
    home = (WEBSITE / "index.html").read_text(encoding="utf-8")
    features = (WEBSITE / "features.html").read_text(encoding="utf-8")
    licensing = (WEBSITE / "pricing.html").read_text(encoding="utf-8")

    assert "lummenna-icon.png" in home
    assert "lummenna-feature-strip.png" in features
    assert "lummenna-pro-banner.png" in licensing


def test_every_referenced_local_asset_exists():
    references: set[str] = set()
    for page_name in PAGES:
        page = (WEBSITE / page_name).read_text(encoding="utf-8")
        references.update(re.findall(r'(?:src|href)="(/assets/[^"]+)"', page))

    assert references
    for reference in references:
        assert (WEBSITE / reference.removeprefix("/")).is_file(), reference


def test_removed_marketing_claims_do_not_return():
    site_text = "\n".join((WEBSITE / page).read_text(encoding="utf-8") for page in PAGES)
    for unsupported_claim in (
        "lummenna_recall",
        "/v1/recall",
        "$199",
        "$699",
        "$1,999",
        "p50 Latency Hybrid",
        "100% Data Sovereignty",
    ):
        assert unsupported_claim not in site_text


def test_no_public_link_points_at_the_private_repository():
    """Anchors must not send visitors to a repository they cannot open.

    QuantmindSSI/lummenna is private, so every github.com anchor returned 404
    for anonymous visitors, including the hero call to action and the
    "view benchmark artifacts" button. A bare ``git clone`` line inside a code
    block is fine: it is an instruction for someone who has been granted
    access, not a link that promises to work.
    """
    anchor_href = re.compile(r'<a\b[^>]*\bhref="([^"]+)"', re.IGNORECASE)
    for page_name in PAGES:
        page = (WEBSITE / page_name).read_text(encoding="utf-8")
        for target in anchor_href.findall(page):
            assert "github.com" not in target, f"{page_name} links to {target}"


def test_pages_do_not_promise_public_artifacts():
    """Copy must not assert that source or artifacts are publicly available."""
    site_text = "\n".join((WEBSITE / page).read_text(encoding="utf-8") for page in PAGES)
    for unsupported_claim in (
        "available in the repository",
        "Inspect the source",
        "View benchmark artifacts",
    ):
        assert unsupported_claim not in site_text, unsupported_claim


def test_pages_do_not_expose_internal_build_history():
    """Visitor-facing copy must not narrate the site's own construction.

    Two figure captions explained that artwork had been "retained from the
    previous frontend" and pointed at the feature registry as the real source
    of truth. That is build commentary addressed to maintainers; a customer
    reading it learns only that the page may be out of date with itself.
    """
    site_text = "\n".join((WEBSITE / page).read_text(encoding="utf-8") for page in PAGES)
    for internal_phrase in (
        "previous frontend",
        "retained from",
        "capability source",
        "licensed feature registry",
    ):
        assert internal_phrase not in site_text, internal_phrase


def test_waitlist_form_posts_same_origin_without_javascript():
    """The form must work under the site's own Content-Security-Policy.

    The policy is `script-src 'none'; connect-src 'none'; form-action 'self'`,
    so the form has to be a native POST to a same-origin path. An absolute URL
    or a fetch() would be blocked at the browser, silently, after deploy.
    """
    home = (WEBSITE / "index.html").read_text(encoding="utf-8")
    form = re.search(r"<form[^>]*class=\"signup\"[^>]*>", home)
    assert form is not None, "waitlist form is missing"
    assert 'method="POST"' in form.group(0)
    action = re.search(r'action="([^"]+)"', form.group(0)).group(1)
    assert action.startswith("/"), f"form action must be same-origin, got {action}"
    assert "<script" not in home


def test_waitlist_form_requires_explicit_consent_and_traps_bots():
    home = (WEBSITE / "index.html").read_text(encoding="utf-8")
    consent = re.search(r'<input[^>]*name="consent"[^>]*>', home)
    assert consent is not None, "consent checkbox is missing"
    # Unticked by default: a pre-ticked box is not consent under UK GDPR.
    assert "checked" not in consent.group(0)
    assert 'type="checkbox"' in consent.group(0)
    assert "required" in consent.group(0)

    honeypot = re.search(r'<input[^>]*name="company"[^>]*>', home)
    assert honeypot is not None, "honeypot field is missing"
    assert 'tabindex="-1"' in honeypot.group(0)

    email = re.search(r'<input[^>]*name="email"[^>]*>', home)
    assert email is not None and 'type="email"' in email.group(0)
    assert "required" in email.group(0)
    assert 'maxlength="254"' in email.group(0), "RFC 5321 caps the path at 254"


def test_waitlist_states_the_software_is_not_yet_published():
    """Copy must not imply an immediate download while none exists."""
    home = (WEBSITE / "index.html").read_text(encoding="utf-8")
    assert "Not yet published" in home
    for overpromise in ("Download now", "Get your download link", "Download Lummenna"):
        assert overpromise not in home, overpromise


def test_waitlist_input_avoids_the_ios_zoom_threshold():
    css = stylesheet_source(WEBSITE / "assets" / "css" / "site.css")
    rule = re.search(r'\.signup input\[type="email"\] \{([^}]*)\}', css)
    assert rule is not None
    # Below 16px iOS Safari zooms on focus and never zooms back out.
    assert "font-size: 16px" in rule.group(1)


def test_public_evidence_matches_committed_artifacts():
    summary = load_evaluation_summary()
    home = (WEBSITE / "index.html").read_text(encoding="utf-8")
    features = (WEBSITE / "features.html").read_text(encoding="utf-8")

    assert f'{summary["e2e_memory"]["recall_at_1"]:.3f}' in home
    assert f'{summary["beir_subset"]["hybrid_ndcg_at_10_macro"]:.3f}' in home
    assert f'{summary["retrieval"]["latency_mean_ms"]["hybrid"]:.1f} ms' in features


def test_every_public_surface_declares_the_same_theme_palette():
    for path in PUBLIC_STYLESHEETS:
        text = path.read_text(encoding="utf-8")
        for token, value in THEME_TOKENS.items():
            assert f"{token}: {value};" in text, f"{path.name} is missing {token}: {value}"


def test_public_surfaces_share_one_typographic_baseline():
    for path in PUBLIC_STYLESHEETS:
        text = path.read_text(encoding="utf-8")
        assert "-webkit-font-smoothing: antialiased;" in text, path.name
        assert "line-height: 1.55;" in text, path.name
        # iOS Safari inflates text on rotation unless this is pinned, which
        # would resize one surface and not the others.
        assert "-webkit-text-size-adjust: 100%;" in text, path.name


def test_public_surfaces_avoid_css_that_resolves_differently_per_engine():
    for path in PUBLIC_STYLESHEETS:
        text = stylesheet_source(path)
        # Measured: `ui-monospace` resolves to SF Mono in Blink but to a
        # different face in WebKit, so naming it makes the engines disagree.
        assert "ui-monospace" not in text, f"{path.name} reintroduces ui-monospace"
        match = FONT_SHORTHAND_WITH_VAR.search(text)
        assert (
            match is None
        ), f"{path.name} uses a font shorthand: {match.group(0) if match else ''}"


def test_masked_backdrops_keep_the_webkit_prefixed_fallback():
    for path in PUBLIC_STYLESHEETS:
        text = stylesheet_source(path)
        standard = len(re.findall(r"(?<!-webkit-)mask-image:", text))
        prefixed = len(re.findall(r"-webkit-mask-image:", text))
        # Safari only understood the prefixed property before 15.4; without it
        # the backdrop covers the whole panel instead of fading out.
        assert prefixed == standard, f"{path.name}: {standard} masks, {prefixed} prefixed"


def test_disclosure_widgets_suppress_both_native_markers():
    for path in PUBLIC_STYLESHEETS:
        text = stylesheet_source(path)
        for rule in re.findall(r"[^{}]*summary\s*\{[^}]*\}", text):
            if "cursor: pointer" not in rule:
                continue
            selector = rule.split("{")[0].strip()
            # Chrome draws the triangle with ::marker, Safari with
            # ::-webkit-details-marker. Hiding only one leaves the engines
            # showing different controls.
            assert "list-style: none" in rule, f"{path.name}: {selector} keeps ::marker"
            assert f"{selector}::-webkit-details-marker" in text, f"{path.name}: {selector}"


def test_every_public_page_opts_into_the_display_safe_area():
    for path in PUBLIC_HTML:
        page = path.read_text(encoding="utf-8")
        # Without viewport-fit=cover iOS Safari letterboxes a notched device in
        # landscape instead of filling the display.
        assert "viewport-fit=cover" in page, path.name


def test_public_surfaces_pad_for_the_notch_and_never_force_a_minimum_width():
    for path in PUBLIC_STYLESHEETS:
        text = stylesheet_source(path)
        assert "env(safe-area-inset-left)" in text, path.name
        assert "env(safe-area-inset-right)" in text, path.name
        # A floor wider than the usable viewport forces sideways scrolling.
        # WebKit reserves ~15px for a classic scrollbar where Blink overlays
        # it, so `min-width: 320px` broke Safari at 320px and not Chrome.
        assert "min-width: 320px" not in text, f"{path.name} floors the body width"


def test_touch_sized_controls_do_not_trigger_ios_zoom():
    text = stylesheet_source(ROOT / "lummenna" / "api" / "dashboard.html")
    coarse = re.search(r"@media \(pointer: coarse\) \{(.+?)\n    \}", text, re.DOTALL)
    assert coarse is not None, "dashboard has no coarse-pointer rule"
    # iOS Safari zooms in on focus below 16px and does not zoom back out.
    assert "font-size: 16px;" in coarse.group(1)
    for control in (".search-controls input", ".credential-field input"):
        assert control in coarse.group(1), control


def test_inline_code_wraps_while_code_blocks_stay_scrollable():
    text = stylesheet_source(WEBSITE / "assets" / "css" / "site.css")
    inline = re.search(r"\ncode \{([^}]*)\}", text)
    block = re.search(r"\npre code \{([^}]*)\}", text)
    assert inline is not None and block is not None
    # A long env var name in prose would otherwise push the page sideways.
    assert "overflow-wrap: anywhere" in inline.group(1)
    # Commands inside a listing must stay intact and scroll instead.
    assert "overflow-wrap: normal" in block.group(1)


def parse_cloudflare_headers(text: str) -> dict[str, dict[str, str]]:
    """Parse a Cloudflare Pages ``_headers`` file.

    An unindented line opens a path block; indented ``Name: value`` lines add
    headers to the block that is currently open. Comments and blank lines are
    ignored.
    """
    blocks: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[0].isspace():
            current = raw.strip()
            blocks.setdefault(current, {})
            continue
        assert current is not None, f"header line before any path block: {raw!r}"
        name, _, value = raw.strip().partition(":")
        blocks[current][name.strip()] = value.strip()
    return blocks


def parse_cloudflare_redirects(text: str) -> dict[str, tuple[str, int]]:
    """Parse a Cloudflare Pages ``_redirects`` file into source -> (target, code)."""
    rules: dict[str, tuple[str, int]] = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split()
        assert len(parts) == 3, f"expected 'source target code', got {raw!r}"
        rules[parts[0]] = (parts[1], int(parts[2]))
    return rules


def test_cloudflare_pages_config_mirrors_the_azure_config():
    """The two platform configs must declare the same policy.

    lummenna.com is served by the Cloudflare Pages project ``lummenna-site``,
    which ignores ``staticwebapp.config.json`` entirely. If the pair drifts, the
    site silently loses its security headers on the platform that actually
    serves it while the Azure-shaped test keeps passing.
    """
    config = json.loads((WEBSITE / "staticwebapp.config.json").read_text(encoding="utf-8"))
    headers = parse_cloudflare_headers((WEBSITE / "_headers").read_text(encoding="utf-8"))
    redirects = parse_cloudflare_redirects((WEBSITE / "_redirects").read_text(encoding="utf-8"))

    assert headers.get("/*") == config["globalHeaders"], "global header blocks differ"

    for route in config["routes"]:
        if "headers" in route:
            assert headers.get(route["route"]) == route["headers"], route["route"]
        if "redirect" in route:
            expected = (route["redirect"], route["statusCode"])
            assert redirects.get(route["route"]) == expected, route["route"]

    declared_redirects = {r["route"] for r in config["routes"] if "redirect" in r}
    assert set(redirects) == declared_redirects, "redirect sets differ"

    declared_header_routes = {r["route"] for r in config["routes"] if "headers" in r}
    assert set(headers) == declared_header_routes | {"/*"}, "header path sets differ"


def test_cloudflare_headers_do_not_weaken_the_content_security_policy():
    headers = parse_cloudflare_headers((WEBSITE / "_headers").read_text(encoding="utf-8"))
    csp = headers["/*"]["Content-Security-Policy"]
    # The site ships no JavaScript; anything looser than this is a regression
    # against what production already serves.
    assert "script-src 'none'" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp


def test_azure_static_site_routes_and_security_headers():
    config = json.loads((WEBSITE / "staticwebapp.config.json").read_text(encoding="utf-8"))
    rewrites = {
        route["route"]: route["rewrite"] for route in config["routes"] if "rewrite" in route
    }
    redirects = {
        route["route"]: (route["redirect"], route["statusCode"])
        for route in config["routes"]
        if "redirect" in route
    }

    assert rewrites == {
        "/features": "/features.html",
        "/pricing": "/pricing.html",
        "/docs": "/docs.html",
    }
    assert redirects == {
        "/index.html": ("/", 301),
        "/features.html": ("/features", 301),
        "/pricing.html": ("/pricing", 301),
        "/docs.html": ("/docs", 301),
    }
    assert config["responseOverrides"]["404"]["statusCode"] == 404
    assert config["trailingSlash"] == "never"
    image_route = next(route for route in config["routes"] if route["route"] == "/assets/img/*")
    assert image_route["headers"]["Cache-Control"] == "public, max-age=604800"
    csp = config["globalHeaders"]["Content-Security-Policy"]
    assert "script-src 'none'" in csp
    assert "unsafe-inline" not in csp
