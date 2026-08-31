"""Pure-function tests for the rendered-link store. No DB, no Cloudinary.

RUN:  python -m pytest tests/test_link_shot_store.py -q
"""

import os
import pytest
from app.services import link_shot_store as S


# --- the flag ---------------------------------------------------------------

def test_off_by_default(monkeypatch):
    monkeypatch.delenv("LINK_SHOT_STORE", raising=False)
    assert not S.enabled()


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_every_off_spelling_is_off(monkeypatch, value):
    monkeypatch.setenv("LINK_SHOT_STORE", value)
    assert not S.enabled()


def test_anything_else_is_on(monkeypatch):
    monkeypatch.setenv("LINK_SHOT_STORE", "1")
    assert S.enabled()


def test_remember_writes_nothing_while_off(monkeypatch):
    monkeypatch.setenv("LINK_SHOT_STORE", "0")
    S.set_target("assignment", 29, 558)
    assert S.remember("https://x.test", shot_b64="abc") == ""


# --- the target -------------------------------------------------------------

def test_no_target_means_no_row(monkeypatch):
    monkeypatch.setenv("LINK_SHOT_STORE", "1")
    S.clear_target()
    assert S.remember("https://x.test", shot_b64="abc") == "", \
        "an ad-hoc render or a staff canary must never write a row"


def test_target_round_trips():
    S.set_target("assignment", 29, 558, submission_id=9375)
    assert S.current_target() == ("assignment", 29, 558, 9375)
    S.clear_target()
    assert S.current_target() is None


def test_submission_id_is_optional():
    S.set_target("assignment", 29, 558)
    assert S.current_target() == ("assignment", 29, 558, None)
    S.clear_target()


# --- url_hash ---------------------------------------------------------------

def test_hash_is_stable():
    assert S.url_hash("https://gamma.app/docs/x") == S.url_hash("https://gamma.app/docs/x")


def test_hash_ignores_surrounding_whitespace():
    assert S.url_hash("  https://a.test  ") == S.url_hash("https://a.test")


def test_different_urls_hash_differently():
    assert S.url_hash("https://a.test") != S.url_hash("https://b.test")


def test_a_query_string_is_part_of_the_url():
    assert S.url_hash("https://a.test?x=1") != S.url_hash("https://a.test?x=2")


def test_hash_is_index_sized():
    assert len(S.url_hash("https://" + "y" * 900)) == 40, \
        "a 900-character LinkedIn share URL still keys a CHAR(40) column"


def test_hash_survives_none():
    assert len(S.url_hash(None)) == 40


# --- public_id --------------------------------------------------------------

def test_public_id_is_stable_so_a_resubmit_overwrites():
    a = S.public_id("assignment", 29, 558, "https://a.test")
    b = S.public_id("assignment", 29, 558, "https://a.test")
    assert a == b


def test_public_id_separates_students():
    assert S.public_id("assignment", 29, 558, "https://a.test") \
        != S.public_id("assignment", 29, 559, "https://a.test")


def test_public_id_separates_assignments():
    assert S.public_id("assignment", 29, 558, "https://a.test") \
        != S.public_id("assignment", 30, 558, "https://a.test")


def test_public_id_separates_links_within_one_submission():
    assert S.public_id("assignment", 29, 558, "https://a.test") \
        != S.public_id("assignment", 29, 558, "https://b.test")


def test_public_id_sits_under_the_folder():
    assert S.public_id("assignment", 29, 558, "https://a.test").startswith(S.FOLDER + "/")


# --- worth_recording --------------------------------------------------------

def test_a_picture_is_worth_recording():
    assert S.worth_recording("base64data", "")


def test_a_FAILURE_is_worth_recording():
    assert S.worth_recording("", "the page was still showing a human-check"), \
        "downstream must know a link could not be opened, not just guess"


def test_silence_is_not_worth_recording():
    assert not S.worth_recording("", "")
    assert not S.worth_recording("   ", "  ")


def test_none_is_not_worth_recording():
    assert not S.worth_recording(None, None)


# --- cloudinary_ready -------------------------------------------------------

def test_missing_credentials_are_not_ready(monkeypatch):
    for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert not S.cloudinary_ready()


def test_partial_credentials_are_not_ready(monkeypatch):
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "c")
    monkeypatch.setenv("CLOUDINARY_API_KEY", "k")
    monkeypatch.delenv("CLOUDINARY_API_SECRET", raising=False)
    assert not S.cloudinary_ready()


def test_all_three_are_ready(monkeypatch):
    for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        monkeypatch.setenv(k, "x")
    assert S.cloudinary_ready()


def test_upload_without_credentials_returns_empty(monkeypatch):
    for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert S._upload("abc", "assignment", 29, 558, "https://a.test") == ""
