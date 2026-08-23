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
    "claude.ai":    ("Claude artifact links only open in a browser, so ALSO attach "
                     "a screenshot of your work, or the HTML file from the "
                     "artifact's Download option."),
    "gamma.app":    ("In Gamma open Share, turn on public access, then copy the "
                     "link."),
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


def nothing_submitted() -> str:
    """No text, no file, nothing to read."""
    return ("We could not find any answer for this assignment. Attach your work "
            "(PDF, Word, Excel, image or text), or type your answer in the box, "
            "then submit again. " + NO_MARK)


def file_unreadable(file_error: str = "", file_name: str = "") -> str:
    """A file arrived but no text came out of it."""
    what = f" ({file_error})" if file_error else ""
    named = f" \"{file_name}\"" if file_name else ""
    return (f"We received your file{named} but could not read any text from it{what}. "
            f"Check that it opens on your own computer, then re-attach it — or type "
            f"your answer in the box — and submit again. If it is a photo, make sure "
            f"the text in it is in focus and right way up. " + NO_MARK)


def too_little_content(word_count: int, file_error: str = "",
                       had_attachment: bool = False) -> str:
    """Enough arrived to store, not enough to judge."""
    found = f"{word_count} word{'' if word_count == 1 else 's'} of text"
    if file_error:
        found += f", and your attachment could not be read ({file_error})"
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


def wrong_task(what_it_is: str, task_title: str) -> str:
    """Real work, belonging to a different task."""
    what = what_it_is or "work for a different task"
    return (f"Not graded: what reached us looks like {what}, not the work this "
            f"assignment asked for (\"{task_title}\"). Attach the correct work and "
            f"submit again — nothing is lost, and this attempt has not been counted "
            f"against you. " + NO_MARK)


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
    "media_not_read":     media_not_transcribed,
}
