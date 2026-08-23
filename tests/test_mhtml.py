"""Saved web pages (.mht / .mhtml).

Student 357 submitted one on Day 05 and it read as nothing at all: .mht was in
no extension set, so it fell through to the byte sniffer, which saw mail
headers and gave up. A "Web Page, Complete" save is a MIME archive holding the
page and every asset it referenced.
"""

import email.message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.utils import file_extractor as fx


def make_mht(html: str, with_image: bool = True) -> bytes:
    """A minimal but honest "Web Page, Complete" archive."""
    root = MIMEMultipart("related")
    root["MIME-Version"] = "1.0"
    root["Content-Location"] = "http://example.com/page.html"
    page = MIMEText(html, "html", "utf-8")
    page["Content-Location"] = "http://example.com/page.html"
    root.attach(page)
    if with_image:
        img = email.message.EmailMessage()
        img.set_content(b"\x89PNG\r\n\x1a\n" + b"A" * 5000,
                        maintype="image", subtype="png")
        img["Content-Location"] = "http://example.com/chart.png"
        root.attach(img)
    return root.as_bytes()


DASHBOARD = ("<html><head><title>Q3 Revenue Dashboard</title></head><body>"
             "<h1>Q3 Revenue Dashboard</h1>"
             "<p>Revenue rose from 4.2 crore in Q1 to 6.8 crore in Q3, driven "
             "by the retail lending book. Fee income stayed flat across all "
             "three quarters, which is the weakness this dashboard is meant "
             "to surface for the credit committee.</p></body></html>")


# ── it is recognised ────────────────────────────────────────────────────────

def test_a_saved_page_is_recognised_by_its_headers():
    assert fx.looks_like_mhtml(make_mht(DASHBOARD)) is True


def test_a_plain_html_file_is_not_mistaken_for_one():
    assert fx.looks_like_mhtml(DASHBOARD.encode()) is False


def test_an_ordinary_pdf_is_not_mistaken_for_one():
    assert fx.looks_like_mhtml(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n") is False


# ── it is read ──────────────────────────────────────────────────────────────

def test_the_learners_words_come_out():
    text, why = fx.extract_text_from_bytes(make_mht(DASHBOARD), "dashboard.mht")
    assert why == ""
    assert "6.8 crore" in text and "retail lending book" in text


def test_the_mhtml_extension_reaches_the_right_reader():
    for name in ("saved.mht", "saved.mhtml", "SAVED.MHT"):
        text, why = fx.extract_text_from_bytes(make_mht(DASHBOARD), name)
        assert "6.8 crore" in text, name


def test_a_saved_page_without_its_extension_is_still_read():
    """The sniffer must catch it too — students rename files."""
    text, _ = fx.extract_text_from_bytes(make_mht(DASHBOARD), "dashboard")
    assert "6.8 crore" in text


def test_the_marker_is_told_what_kind_of_thing_this_is():
    text, _ = fx.extract_text_from_bytes(make_mht(DASHBOARD), "d.mht")
    assert "SAVED WEB PAGE" in text


def test_the_page_title_survives():
    text, _ = fx.extract_text_from_bytes(make_mht(DASHBOARD), "d.mht")
    assert "Q3 Revenue Dashboard" in text


# ── the embedded assets do not eat the budget ───────────────────────────────

def test_embedded_images_are_not_dumped_into_the_text():
    """A Complete save embeds every asset as base64. One background image
    would spend the whole text budget before the learner's words were read."""
    text, _ = fx.extract_text_from_bytes(make_mht(DASHBOARD), "d.mht")
    assert "AAAAAAAA" not in text
    assert len(text) < 4000


def test_an_archive_with_no_page_inside_says_so_instead_of_returning_nothing():
    root = MIMEMultipart("related")
    root["MIME-Version"] = "1.0"
    root["Content-Location"] = "http://example.com/x"
    img = email.message.EmailMessage()
    img.set_content(b"\x89PNG\r\n\x1a\n", maintype="image", subtype="png")
    root.attach(img)
    text, why = fx.extract_text_from_bytes(root.as_bytes(), "empty.mht")
    assert text == "" and "no readable page" in why


def test_a_corrupt_archive_fails_honestly_rather_than_crashing():
    text, why = fx.extract_text_from_bytes(b"MIME-Version: 1.0\r\n"
                                           b"Content-Type: multipart/related;"
                                           b" boundary=nope\r\n\r\ngarbage",
                                           "broken.mht")
    assert text == "" and why


def test_several_pages_in_one_archive_are_all_read():
    root = MIMEMultipart("related")
    root["MIME-Version"] = "1.0"
    root["Content-Location"] = "http://example.com/a"
    for body in ("<html><body><p>First frame content about revenue growth "
                 "across the retail book this quarter.</p></body></html>",
                 "<html><body><p>Second frame content about the fee income "
                 "line staying flat all year.</p></body></html>"):
        part = MIMEText(body, "html", "utf-8")
        part["Content-Location"] = "http://example.com/frame"
        root.attach(part)
    text, _ = fx.extract_text_from_bytes(root.as_bytes(), "frames.mht")
    assert "First frame" in text and "Second frame" in text
