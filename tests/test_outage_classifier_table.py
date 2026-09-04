"""WHOSE FAULT WAS IT — the whole table (04 Sep 2026).

grade_guard.reads_as_our_outage() decides whether a failed read is retried
silently (ours) or the learner is told and the row stamped (theirs). Getting
it wrong in either direction is a live incident: "ours" on a learner's dead
link loops the row through every sweep with nobody told; "theirs" on our
provider being down blames a learner in writing for our outage.

This test is the table of every failure string intake can produce today,
with the answer each must get. Add a row whenever a new string is born.
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from app.services import grade_guard as g

THEIRS = [
    # the learner's link, whatever its site answered
    "https://x.example/p: link returned HTTP 503",
    "https://x.example/p: link returned HTTP 500",
    "https://x.example/p: link returned HTTP 404",
    "https://x.example/p: could not open the link (ReadTimeout)",
    "https://x.example/p: could not open the link (ConnectTimeout)",
    "https://x.example/p: could not open the link (ConnectError)",
    "https://x.example/p: link redirected with no destination (HTTP 302)",
    "https://x.example/p: link redirected too many times",
    "https://app.notion.com/p/abc: this link is private — it opens a sign-in page instead of the work",
    # a site that refused our browser: stamped with its own message, never looped
    "https://claude.ai/share/5036b082-ca19: the page was still showing a human-check (Cloudflare) when the browser gave up",
    # the learner's file
    "notes.pdf: file too large (12 MB > 10 MB)",
    "notes.pdf: file exceeds 10240 KB",
    "work.xlsx: xlsx parse error: File is not a zip file",
    "photo.jpg: OCR failed on anthropic: BadRequestError: Error code: 400 - "
    "{'message': 'Could not process image'}, 'request_id': 'req_1'",
    "this docx file has no readable text — if the work is a scan, upload it as an image",
    "rec.m4a: this recording is 45 minutes long; the limit is 30",
]

OURS = [
    "photo.jpg: OCR failed on startupapi: Error code: 503 - provider capacity is temporarily unavailable",
    "photo.jpg: OCR failed on anthropic: Error code: 400 - You have reached your specified API usage limits",
    "photo.jpg: OCR failed: every provider refused the request",
    "rec.mp4: transcription failed (APIConnectionError)",
    "rec.mp4: transcription HTTP 503: upstream",
    "https://x.example/p: link rendering needs the playwright package",
    "https://x.example/p: the browser was busy with another page for longer than 60s",
    "https://x.example/p: the page could not be rendered (TimeoutError)",
    "file.pdf: download HTTP 500 (no Cloudinary credentials to retry)",
    "file.pdf: cloudinary sign failed: KeyError",
    "Model returned no structured result (model=claude-haiku-4-5)",
    # a learner's dead link beside a real outage: still ours
    "https://x.example/p: link returned HTTP 404; photo.jpg: OCR failed on anthropic: Error code: 503",
]


@pytest.mark.parametrize("why", THEIRS)
def test_the_learner_is_told(why):
    assert not g.reads_as_our_outage(why), why


@pytest.mark.parametrize("why", OURS)
def test_we_retry_quietly(why):
    assert g.reads_as_our_outage(why), why


def test_link_statuses_reach_the_learner_verbatim_and_exceptions_do_not():
    assert g.learner_facing("https://x/p: link returned HTTP 404") == "link returned HTTP 404"
    assert g.learner_facing("https://x/p: could not open the link (ReadTimeout)") == "the link did not respond"
    assert g.learner_facing("work.xlsx: xlsx parse error: File is not a zip file") == "the file could not be read"
