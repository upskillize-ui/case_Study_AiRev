"""A link id is not an HTTP status (04 Sep 2026, job 852 live).

reads_as_our_outage() looks for "429", "500", "502", "503", "504" in the
failure text — and the failure text names the link it is about. claude.ai
share ids are hex: `5036b082…` matched 503, `2b2fcce9-7504…` matched 504,
`d5364b6c-e44e-429a…` matched 429. Every Cloudflare-refused claude.ai link
whose id happened to contain one of those triples was filed as OUR outage:
the learner was never told to add a screenshot, and the row came back to the
renderer (three reloads, about a minute) on every sweep.

Two rules pinned here: URLs are stripped before markers are matched, and a
reader-blocked verdict is never an outage, whatever else the text says.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from app.services import grade_guard as g

BLOCKED = "the page was still showing a human-check (Cloudflare) when the browser gave up"


def test_hex_ids_containing_status_digits_are_not_outages():
    for url in ("https://claude.ai/share/5036b082-ca19-4b4f-a201-0f6b26791660",
                "https://claude.ai/public/artifacts/2b2fcce9-7504-4156-9393-856b6e91058b",
                "https://claude.ai/public/artifacts/d5364b6c-e44e-429a-8c26-1294a5840616"):
        why = f"{url}: {BLOCKED}"
        assert g.reader_blocked(why)
        assert not g.reads_as_our_outage(why), url


def test_a_url_with_a_real_looking_code_in_its_path_is_still_not_an_outage():
    assert not g.reads_as_our_outage(
        "https://example.com/error/503/page: link returned HTTP 404")


def test_a_real_status_code_outside_the_url_still_reads_as_ours():
    assert g.reads_as_our_outage(
        "OCR failed on anthropic: Error code: 503 - provider capacity unavailable")
    assert g.reads_as_our_outage(
        "https://x.example/doc: fetch failed: HTTP 503 Service Unavailable")


def test_reader_blocked_beats_every_other_marker():
    # Even wording that would otherwise read as transport failure is a
    # reader limit when the site refused the browser.
    assert not g.reads_as_our_outage(
        "connection timed out while the page was still showing a human-check (Cloudflare)")


def test_a_broken_image_is_the_learners_file_not_our_outage():
    why = ("inbound8232410315706722562.jpg: OCR failed on anthropic: BadRequestError: "
           "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
           "'message': 'Could not process image'}, 'request_id': 'req_011Cehx6q9YFyqB2fAG2LWtt'}")
    assert not g.reads_as_our_outage(why)
    # …and the learner still never sees the machinery.
    assert g.learner_facing(why) == "the file could not be read"


def test_a_provider_that_is_actually_down_is_still_ours():
    assert g.reads_as_our_outage(
        "OCR failed on anthropic: Error code: 400 - You have reached your specified API usage limits")
