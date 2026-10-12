from __future__ import annotations

from app.email.outlook import _strip_html


def test_strip_html_drops_hidden_display_none_spans():
    """Google Calendar's structured-data span carries the event name hidden
    from view, immediately followed by the real visible text with no
    whitespace in the markup — a tag-only stripper glues them into one run-on
    word ("Interview with PicogridThis meeting...") that corrupts whatever
    later reads the company name out of it."""
    html = (
        '<span style="display:none">Interview with Picogrid</span>'
        "This meeting will take place over the phone."
    )
    assert _strip_html(html) == "This meeting will take place over the phone."


def test_strip_html_drops_hidden_variants():
    html = (
        '<span style="visibility: hidden">secret</span>'
        '<span aria-hidden="true">also hidden</span>'
        "<span hidden>still hidden</span>"
        "Visible text"
    )
    assert _strip_html(html) == "Visible text"


def test_strip_html_drops_style_and_script_content():
    """The old stripper kept CSS/JS text verbatim, which is why many Outlook
    emails' body_text started with a wall of raw "body, table { font-family:
    ... }" before any real content."""
    html = (
        "<style>body, table { font-family: Verdana, Arial, sans-serif; }</style>"
        "<script>console.log('tracking');</script>"
        "<p>Hello Thomas</p>"
    )
    assert _strip_html(html) == "Hello Thomas"


def test_strip_html_separates_block_elements():
    html = "<div>Line one</div><div>Line two</div><p>Line three</p>"
    assert _strip_html(html) == "Line one\n\nLine two\n\nLine three"


def test_strip_html_decodes_entities():
    html = "<p>Thank you &amp; good luck&nbsp;&mdash; team</p>"
    assert "&amp;" not in _strip_html(html)
    assert "Thank you & good luck" in _strip_html(html)


def test_strip_html_survives_malformed_markup():
    html = "<div>Unclosed tag <span>text"
    assert "text" in _strip_html(html)
