# app/services/student_notices.py
# ---------------------------------------------------------------------------
# WHAT THE LEARNER READS WHEN WE CANNOT MARK THEIR WORK.
#
# Why this module exists (23 Aug 2026). Every "not graded" message was an
# inline string in a route. There were seven of them, written on different
# days, and they disagreed:
#   - some said "no marks are recorded", others said nothing about the mark;
#   - the published-link message told EVERY learner to use Notion's Share ->
#     Publish, including learners whose link was a Google Doc or a Gemini share;
#   - the submit path and the regrade path phrased the same refusal differently,
#     so a learner who re-submitted got a different explanation for one fault.
#
# Ranjana's rule (19 Aug 2026): "if it student side fault show them what is the
# issue so they can re-submit or next time don't repeat same issue." A message
# that only says "not graded" teaches nothing. Every notice here follows the
# same three beats, in this order:
#
#   1. WHAT HAPPENED   — plainly, without blaming them for our reach
#   2. WHAT TO DO      — the exact clicks, for THEIR tool, not a generic one
#   3. WHERE THEY STAND— whether a mark was recorded, so nobody guesses
#
# Voice: coaching, never punitive. No emojis. Second person. The learner has
# usually done the work; something about the delivery stopped it reaching us.
# ---------------------------------------------------------------------------

import re
from urllib.parse import urlparse

NO_MARK = "No marks have been recorded for this attempt yet."

# Per-host publishing instructions. A learner following Notion's steps on a
# Google Drive link is a learner we have failed twice.
_HOST_STEPS = {
    "notion.site": ("Open your page in Notion, click Share, then Publish, and copy "
                    "the published link (it looks like yourname.notion.site/...)."),
    "notion.so":   ("Open your page in Notion, click Share, then Publish, and copy "
                    "the published link (it looks like yourname.notion.site/...)."),
    "docs.google.com":   ("Open the file, click Share, change General access to "
                          "\"Anyone with the link\", then copy that link."),
    "drive.google.com":  ("Open the file, click Share, change General access to "
                          "\"Anyone with the link\", then copy that link."),
    "share.gemini.google": ("In Gemini open the chat, use Share, create a public "
                            "link, and copy that. A link that still needs your "
                            "Google account will not open for us."),
    "gemini.google.com":   ("In Gemini open the chat, use Share, create a public "
                            "link, and copy that. A link that still needs your "
                            "Google account will not open for us."),
    "notebooklm.google.com": ("In NotebookLM click Share, set access to anyone with "
                              "the link, then copy it. You can also attach the "
                              "Audio Overview or your notes directly."),
    "claude.site":  ("Claude artifact links only open in a browser, so ALSO attach "
                     "a screenshot of your work, or the HTML file from the "
                     "artifact's Download option."),
    "claude.ai":    ("A claude.ai/chat link only opens for your own account. In "
                     "Claude open the artifact, click Publish, and submit the "
                     "claude.ai/public/artifacts link — and ALSO attach a "
                     "screenshot of your work, or the HTML file from the "
                     "artifact's Download option."),
    "gamma.app":    ("In Gamma open Share, turn on public access, then copy the "
                     "link."),
    "figma.com":    ("In Figma click Share, change \"Only people invited\" to "
                     "\"Anyone with the link\" (can view), then copy the link."),
    "miro.com":     ("In Miro click Share, set \"Anyone with the link\" to can "
                     "view, then copy the link."),
    "canva.com":    ("In Canva click Share, choose \"Anyone with the link\", set it "
                     "to view, then copy the link."),
    "lovable.app":  ("Open your project, publish it, and copy the published URL."),
    "github.com":   ("Make the repository public, or attach the files directly."),
}

_GENERIC_STEPS = ("Open the link in a private/incognito window. If it asks you to "
                  "sign in, it is not public yet — change its sharing setting to "
                  "anyone with the link, then submit the new link.")


def publish_steps(url: str) -> str:
    """The exact clicks for the host this learner actually used. Pure.

    Falls back to the incognito test, which teaches the underlying idea: a link
    only works for us if it works for a stranger.
    """
    host = (urlparse(str(url or "")).hostname or "").lower().lstrip(".")
    if not host:
        return _GENERIC_STEPS
    for known, steps in _HOST_STEPS.items():
        if host == known or host.endswith("." + known) or known in host:
            return steps
    return _GENERIC_STEPS


# ---------------------------------------------------------------------------
# NOTHING TECHNICAL REACHES A LEARNER (23 Aug 2026).
#
# Ranjana: "students do not get confused or be in problem — whatever is
# happening they should know in an understandable and polished version. Do not
# tell backend or developer issue as msg them."
#
# She is right, and it was live. Verbatim, from these very functions:
#
#   "...could not read any text from it (HTTP 403: forbidden)"
#   "...could not be read (base64 decode failed: BinasciiError)"
#   "...could not be rendered (TimeoutError)"
#
# A learner reading that learns nothing except that something is broken and
# it might be their fault. Every internal reason now passes through
# plain_reason() first: it says what happened in words a student can act on,
# and where the fault is OURS it says so, because a learner should never be
# left thinking they broke it.
#
# Ordered longest-match-first is not needed — each pattern is a distinct
# fingerprint — but the fallback IS deliberate: an unrecognised reason
# becomes a plain honest sentence rather than being passed through raw.
# ---------------------------------------------------------------------------

_PLAIN = (
    # ours — say so plainly
    (("403", "forbidden", "401", "unauthorized", "permission denied"),
     "we were not allowed to open it from our side"),
    (("404", "not found", "no longer exists", "gone"),
     "we could not find it — it was no longer there when we looked"),
    (("timeout", "timed out", "readtimeout", "connecttimeout"),
     "it took too long to open and we had to stop waiting"),
    (("could not be rendered", "render failed", "browser", "playwright"),
     "our reader could not open it"),
    (("500", "502", "503", "504", "server error", "connection"),
     "we could not reach the service that stores it"),
    # the file itself
    (("password", "encrypted", "decrypt"),
     "it is password protected, so it could not be opened"),
    (("corrupt", "invalid pdf", "bad zip", "not a valid", "damaged",
      "binascii", "decode failed", "unicodedecode"),
     "the file appears to be damaged"),
    (("empty", "0 bytes", "no content", "blank"),
     "the file was empty"),
    (("no text layer", "ocr found nothing", "scanned", "no readable text"),
     "no text could be read from it — it may be a photo of something blank, "
     "or too blurred to read"),
    (("too large", "exceeds", "size limit", "ceiling"),
     "it is larger than we can open"),
    (("unsupported", "cannot be read as text", "not a file", "format"),
     "this file type could not be opened"),
)

_FALLBACK_REASON = "it could not be opened"


def plain_reason(raw: str) -> str:
    """An internal reason, in words a learner can act on. Pure.

    Never returns a status code, an exception name, a library name or a
    stack fragment. An unrecognised reason becomes an honest plain sentence
    rather than being passed through — passing it through is the bug.
    """
    text = (raw or "").strip().lower()
    if not text:
        return ""
    for fingerprints, plain in _PLAIN:
        if any(f in text for f in fingerprints):
            return plain
    return _FALLBACK_REASON


def queued_for_review() -> str:
    """What a learner sees the moment they submit.

    They must never be left watching a spinner, and they must know it is safe
    to close the page — the review runs whether they are there or not.
    """
    return ("Your work is in. Your feedback is being prepared and will appear "
            "here shortly — usually within a few minutes. You can close this "
            "page; it will be waiting for you when you come back.")


def still_being_reviewed() -> str:
    """What they see if they return before the review has finished."""
    return ("Your feedback is still being prepared. Nothing is wrong and "
            "nothing is lost — check back in a few minutes.")


def nothing_submitted() -> str:
    """No text, no file, nothing to read."""
    return ("We could not find any answer for this assignment. Attach your work "
            "(PDF, Word, Excel, image or text), or type your answer in the box, "
            "then submit again. " + NO_MARK)


# Formats nothing on our side can open, and the one thing to do about each.
# "Re-attach it" is useless advice for a .fig — the file will fail again. Name
# the export that works, so the learner fixes it in one attempt.
_FORMAT_FIX = {
    "fig": "Export your screens from Figma as PNG or JPG and upload those.",
    "psd": "Export as PNG or JPG and upload that.",
    "ai": "Export as PNG or PDF and upload that.",
    "sketch": "Export as PNG or JPG and upload that.",
    "rar": "Upload a ZIP instead, or attach the files themselves.",
    "7z": "Upload a ZIP instead, or attach the files themselves.",
    "mht": "Save the page as a PDF and upload that.",
    "mhtml": "Save the page as a PDF and upload that.",
    "webarchive": "Save the page as a PDF and upload that.",
    "exe": "Upload your work as a document, an image or a link.",
}


def format_fix(file_name: str = "") -> str:
    """The one step that will actually work for this file type. Pure."""
    ext = (file_name or "").rsplit(".", 1)[-1].lower() if "." in (file_name or "") else ""
    return _FORMAT_FIX.get(ext, "")


def file_unreadable(file_error: str = "", file_name: str = "") -> str:
    """A file arrived but no text came out of it."""
    plain = plain_reason(file_error)
    what = f" — {plain}" if plain else ""
    named = f" \"{file_name}\"" if file_name else ""
    fix = format_fix(file_name)
    if fix:
        return (f"We received your file{named} but we cannot open that file type. "
                f"{fix} Your work is safe — nothing you submitted is lost. " + NO_MARK)
    return (f"We received your file{named} but could not read any text from it{what}. "
            f"Check that it opens on your own computer, then re-attach it — or type "
            f"your answer in the box — and submit again. If it is a photo, make sure "
            f"the text in it is in focus and right way up. " + NO_MARK)


def too_little_content(word_count: int, file_error: str = "",
                       had_attachment: bool = False) -> str:
    """Enough arrived to store, not enough to judge."""
    found = f"{word_count} word{'' if word_count == 1 else 's'} of text"
    if file_error:
        found += (f", and your attachment could not be read — "
                  f"{plain_reason(file_error)}")
    elif had_attachment:
        found += ", and no readable text could be taken from your attachment"
    else:
        found += ", and no file was attached"
    return (f"We have not scored this yet — we could only find {found}. "
            f"If your work is in a file, re-attach it (PDF, Word, image or text); "
            f"if it is written work, put your reasoning in the answer box. "
            + NO_MARK)


def link_never_opened(url: str = "") -> str:
    """A publish-this-task whose link asked us to sign in, or never loaded.

    This is the message that replaced a MARK. Twenty-one Day-04 learners were
    given scores between 0.0 and 4.7 for pages we simply could not open; the
    grade measured our reach, not their work.
    """
    return ("Your link did not open for us — it asks whoever visits it to sign in, "
            "so your page could not be read. " + publish_steps(url) + " "
            "Submit that link and you will be marked normally. " + NO_MARK)


def link_opens_only_in_a_browser(url: str = "") -> str:
    """The artefact exists but its page renders only for a signed-in human."""
    return ("We can see you submitted your work, but we could not open it from our "
            "side. " + publish_steps(url) + " You can also paste a few lines in the "
            "answer box about what you built and how. " + NO_MARK)


def link_missing_entirely() -> str:
    """The row says "Link submission" but carries no URL anywhere."""
    return ("Your submission says a link was shared, but no link reached us — only "
            "the words \"link submission\". Paste the full address, starting with "
            "https://, into the answer box and submit again. " + NO_MARK)


# The marker's identification usually ends with its own contrast — "…, not a
# personal 5-year plan" / "… rather than a Data Science deck". The notice
# already names this assignment, so that tail is dropped; without it the
# sentence read "X, not Y, and this assignment is Y".
_CONTRAST_TAIL = re.compile(r"\s*(?:[,;]|—|–|\s-\s)\s*(?:not|rather than|instead of|unrelated to)\b.*$",
                            re.IGNORECASE)


def _what_arrived(what_it_is: str) -> str:
    """The marker's identification as a noun phrase fit for mid-sentence. Pure."""
    what = _CONTRAST_TAIL.sub("", (what_it_is or "").strip()).strip().rstrip(".").strip()
    if not what:
        return "work for a different assignment"
    # Lower-case a sentence-initial capital ("A poster", "An essay", "Study
    # plan"); an acronym ("PDF document", "IBPS notes") keeps its case.
    if len(what) > 1 and what[0].isupper() and not what[1].isupper():
        what = what[0].lower() + what[1:]
    return what


def wrong_task_points(what_it_is: str, task_title: str, out_of: int = 100) -> list:
    """Real work, belonging to a different task — the three lines the learner
    reads, in order: what arrived · where the marks stand · what to do. Pure.

    ZERO, SAID PLAINLY (04 Sep 2026, Ranjana). Day 17's poster sent for
    Day 18 is not Day 18's work, and the mark says so: 0. The wording is a
    statement of fact, nothing a learner can argue with or feel told off by —
    no "this looks like", no "read the brief again", no apology either. The
    system's own reading of the file is never mentioned.
    """
    what = _what_arrived(what_it_is)
    return [
        f"This submission is for a different assignment: what reached us is "
        f"{what}, and this assignment is \"{task_title}\".",
        f"Marks for this attempt: 0 out of {out_of}.",
        f"To submit the work for \"{task_title}\", message the Upskillize team "
        f"to reopen it, then submit again.",
    ]


def wrong_task(what_it_is: str, task_title: str, out_of: int = 100) -> str:
    """wrong_task_points as one paragraph."""
    return " ".join(wrong_task_points(what_it_is, task_title, out_of))


# A LINK THAT DID NOT OPEN IS NOT "NOTHING TO READ" (04 Sep 2026, Ranjana:
# six Figma files filed under "link opened, nothing to read" — every one of
# them a sign-in wall). The renderer records WHY a link gave nothing; the
# unassessable notice used to throw that reason away and send a generic
# "would not open for us". These are design apps that draw their sign-in
# box with JavaScript, so a visitor gets no words at all — an empty page
# from one of them is a private link, not an empty one.
_SIGN_IN_APPS = ("figma.com", "canva.com", "miro.com", "lucid.app")
_PRIVATE_MARKS = ("sign-in", "sign in", "log in", "login", "private",
                  "need access", "request access", "you need to sign")
_GONE_MARKS = ("no longer exists", "not found", "404")


def _is_sign_in_app(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower().lstrip(".")
    return any(host == h or host.endswith("." + h) for h in _SIGN_IN_APPS)


def unreadable_link_notice(url: str, why: str) -> str:
    """The learner's notice for ONE link that gave us nothing, chosen from
    the reader's own reason. "" when the reason is not one we can name —
    the caller then falls back to the generic wording. Pure."""
    low = (why or "").lower()
    if any(m in low for m in _PRIVATE_MARKS):
        return link_never_opened(url)
    if "rendered empty" in low and _is_sign_in_app(url):
        return link_never_opened(url)
    if any(m in low for m in _GONE_MARKS):
        return ("Your link no longer opens — the page is not at that address "
                "any more. Check the link still works for you, or attach the "
                "file itself, then submit again. " + NO_MARK)
    return ""


# Hosts whose bot protection refuses a server outright. Nothing we can do
# from our side, and nothing the learner did wrong — so the notice must ask
# for a different FORM of the work rather than a "public" link they have
# already made public.
#
# Verified live 24 Aug 2026: two published gamma.app decks, both returning
# Cloudflare's human-check to the agent's browser. Day 09's own brief told
# 76 learners to submit "a Gamma share link only (no PDFs or file uploads)",
# which made their work unreadable by instruction.
_BOT_PROTECTED = {
    "gamma.app":   ("Gamma", "In Gamma use Share -> Export -> PDF, and upload "
                             "that file with your link"),
    "canva.com":   ("Canva", "In Canva use Share -> Download -> PDF, and upload "
                             "that file with your link"),
    "figma.com":   ("Figma", "Export your frames as PNG or PDF and upload them "
                             "with your link"),
}


def bot_protected_host(url: str) -> tuple:
    """(tool name, what to attach instead) for a host that refuses servers.

    Returns ("", "") for everything else. Pure.
    """
    host = (urlparse(str(url or "")).hostname or "").lower().lstrip(".")
    for known, (name, how) in _BOT_PROTECTED.items():
        if host == known or host.endswith("." + known):
            return name, how
    return "", ""


def link_blocked_by_the_site(url: str = "") -> str:
    """The site's own bot protection refused us. Not the learner's doing.

    This must never read like the "make your link public" notice: their link
    IS public, and telling them to publish something already published is how
    a learner concludes the system is broken and stops trying.
    """
    name, how = bot_protected_host(url)
    if name:
        return (f"Your link opened for people, but {name}'s security check "
                f"blocks automated readers, so our reviewer could not see your "
                f"work. This is not something you did wrong and your link is "
                f"fine. {how}, then submit again — we will mark it from that. "
                + NO_MARK)
    return ("Your link opened for people, but the site's security check blocks "
            "automated readers, so our reviewer could not see your work. This "
            "is not something you did wrong. Please also attach a PDF export "
            "or a few screenshots of your work and submit again. " + NO_MARK)


def link_is_not_the_work(what_arrived: str, task_title: str) -> str:
    """The link opened, and what it showed was not this task's deliverable.

    Day 07: one learner sent their Day-06 Suno song, another sent Gemini's own
    advertisement page. Both were marked 0.00/10. Both should have been told
    what arrived, so they could send the right link the same evening.
    """
    arrived = (what_arrived or "").strip().rstrip(".")
    saw = f" What we opened was {arrived[0].lower()}{arrived[1:]}." if arrived else ""
    return (f"Your link opened, but it does not show the work this assignment "
            f"asked for (\"{task_title}\").{saw} Send the link to the work "
            f"itself — or attach a screenshot of it — and submit again. "
            + NO_MARK)


def media_not_transcribed(kind: str = "audio") -> str:
    """An Audio or Video Overview we could not turn into text."""
    return (f"Your {kind} file reached us but we could not turn it into text, so "
            f"there was nothing for the marker to read. Add a short written summary "
            f"of what it covers in the answer box — a few lines is enough — and "
            f"submit again with the file. " + NO_MARK)


# Every notice, by the reason code the routes and tools use. One place to read
# the full set, and the thing a test iterates over.
NOTICES = {
    "nothing_submitted":  nothing_submitted,
    "file_unreadable":    file_unreadable,
    "too_little_content": too_little_content,
    "link_never_opened":  link_never_opened,
    "link_browser_only":  link_opens_only_in_a_browser,
    "link_missing":       link_missing_entirely,
    "wrong_task":         wrong_task,
    "queued":             queued_for_review,
    "still_reviewing":    still_being_reviewed,
    "link_not_the_work":  link_is_not_the_work,
    "link_blocked":       link_blocked_by_the_site,
    "media_not_read":     media_not_transcribed,
}
