# tests/test_grading_alignment.py
# ---------------------------------------------------------------------------
# The three grading defects of 02 Sep 2026, each pinned by a test that fails
# on the old code:
#
#   (a) the headline sentence and the score were two independent judgements —
#       "covered 3 of 7" printed beside 0.00 and beside 0.70;
#   (b) an unmatched requirement became a silent zero;
#   (c) artefact submissions were capped at 20% for having nothing to quote
#       and penalised again for a short caption.
#
# Plus the leak: a refusal was skipped by the sweeper for ever.
#
# Pure functions only — no DB, no model, no network.
# ---------------------------------------------------------------------------

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
for _k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_k, None)

from _stubs import import_with_stubs

from app.services import grade_guard, scoring_service

(aggregate, apply_gates, has_nontext_evidence, submitted_kinds,
 submitted_items) = import_with_stubs(
    "app.services.review_pipeline",
    ("aggregate", "apply_gates", "has_nontext_evidence", "submitted_kinds",
     "submitted_items"))

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} — {detail}")
        FAILURES.append(name)


# ─── (c) nothing to quote is not nothing to judge ───────────────────────────

def test_nontext():
    print("\n(c) has_nontext_evidence — lifted only where quoting is impossible")
    prose = " ".join(["banking"] * 200)
    img = "=== ITEM 1: IMAGE (poster.png) ===\n" + prose
    aud = "=== ITEM 1: AUDIO RECORDING (take.m4a) ===\n" + prose
    vid = "=== ITEM 1: VIDEO (demo.mp4) ===\n" + prose
    thin_link = "=== ITEM 1: LINK (https://suno.com/s/x) ===\nSuno. Sign in."
    fat_link = "=== ITEM 1: LINK (https://x.dev/app) ===\n" + prose
    essay_pdf = "=== ITEM 1: DOCUMENT (essay.pdf) ===\n" + prose
    typed = "=== ITEM 1: TYPED TEXT (answer box) ===\n" + prose
    thin_typed = "=== ITEM 1: TYPED TEXT (answer box) ===\nIt was good."

    check("a picture is our rendering — cap off", has_nontext_evidence(img))
    check("a recording is our rendering — cap off", has_nontext_evidence(aud))
    check("a video is our rendering — cap off", has_nontext_evidence(vid))
    check("a page with nothing on it — cap off (was ON: the 2/10 cohort)",
          has_nontext_evidence(thin_link))
    check("an opened page full of text — cap STAYS ON",
          not has_nontext_evidence(fat_link))
    check("a PDF essay is quotable — cap STAYS ON",
          not has_nontext_evidence(essay_pdf))
    check("typed prose — cap STAYS ON", not has_nontext_evidence(typed))
    check("thin typed prose with no artefact — cap STAYS ON",
          not has_nontext_evidence(thin_typed))
    check("a poster beside a long typed answer still cannot be quoted",
          has_nontext_evidence(typed + "\n\n" + img))
    check("passed-in pictures always count", has_nontext_evidence("", images=[{}]))
    check("kinds parsed", submitted_kinds(typed + "\n\n" + thin_link)
          == {"typed text", "link"}, submitted_kinds(typed + "\n\n" + thin_link))
    check("bodies parsed", [k for k, _ in submitted_items(img)] == ["image"])


def test_artefact_not_capped():
    """A poster judged 90% keeps 90% — the cap measured the medium."""
    print("\n(c) the zero-quote cap must not fire on an artefact")
    criteria = [{"name": "Poster is created with an AI tool", "score_pct": 90,
                 "evidence_quotes": [], "judgment": "The poster is here and it is good.",
                 "case_specific": True}]
    rubric = [{"name": "Poster is created with an AI tool", "maxScore": 100}]

    typed_only = apply_gates(criteria, rubric, [], [], [], nontext_evidence=False)
    artefact = apply_gates(criteria, rubric, [], [], [], nontext_evidence=True)
    check("prose with no quotes is still capped at 20",
          typed_only["breakdown"][0]["percentage"] == 20)
    check("an artefact keeps its 90", artefact["breakdown"][0]["percentage"] == 90,
          artefact["breakdown"][0]["percentage"])


def test_no_length_penalty_on_artefacts():
    print("\n(c) a caption is not a short essay")
    gated = {"breakdown": [{"criteria": "Poster", "maxScore": 100,
                            "percentage": 80, "score": 80.0}],
             "total_cap": 100, "error_deduction": 0, "gates_hit": []}
    prose = aggregate(gated, word_count=4, word_limit_min=100, word_limit_max=500)
    artefact = aggregate(gated, word_count=4, word_limit_min=100,
                         word_limit_max=500, artefact_deliverable=True)
    check("prose still penalised for length", prose["wordCountPenalty"] > 0)
    check("artefact not penalised", artefact["wordCountPenalty"] == 0)
    check("artefact keeps the mark", artefact["totalScore"] == 80,
          artefact["totalScore"])


# ─── (b) an unmatched requirement is not a zero ─────────────────────────────

def test_unjudged_not_zero():
    print("\n(b) an unmatched requirement is excluded, not failed")
    criteria = [{"name": "Publishes the app", "score_pct": 80,
                 "evidence_quotes": ["deployed at"], "judgment": "Done.",
                 "case_specific": True}]
    rubric = [{"name": "Publishes the app", "maxScore": 50},
              {"name": "Explains the prompt used", "maxScore": 50}]
    gated = apply_gates(criteria, rubric, [], [], [])
    rows = gated["breakdown"]
    check("judged row is judged", rows[0]["unjudged"] is False)
    check("unmatched row is flagged", rows[1]["unjudged"] is True)
    check("the miss is traced", any(g["gate"] == "unjudged_requirement"
                                    for g in gated["gates_hit"]))

    scores = aggregate(gated, 300, 100, 500)
    # 80% of the judged half, rescaled to the whole: 80, not 40.
    check("scored out of what was judged", scores["totalScore"] == 80,
          scores["totalScore"])
    check("the exclusion is reported", scores["unjudgedRequirements"]
          == ["Explains the prompt used"])


def test_minority_judged_refused():
    print("\n(b) a mark from a minority of the brief is refused")
    rows = [{"requirement": "A", "outOf": 2, "unjudged": False,
             "note": "Done.", "percent": 90},
            {"requirement": "B", "outOf": 4, "unjudged": True},
            {"requirement": "C", "outOf": 4, "unjudged": True}]
    check("share computed on weight",
          abs(grade_guard.judged_share(rows) - 0.2) < 1e-9,
          grade_guard.judged_share(rows))
    allowed, why = grade_guard.may_write_grade(
        criteria=[dict(r, score_pct=r.get("percent")) for r in rows],
        words_read=400, proposed_score=90.0,
        review={"strengths": ["x"], "detailedFeedback": "y"})
    check("guard refuses", not allowed, why)

    ok_rows = [dict(r, unjudged=False, score_pct=80, judgment="Done.") for r in rows]
    allowed2, _ = grade_guard.may_write_grade(
        criteria=ok_rows, words_read=400, proposed_score=80.0,
        review={"strengths": ["x"], "detailedFeedback": "y"})
    check("guard allows a fully judged task", allowed2)

    legacy = [{"criteria": "A", "maxScore": 100, "score_pct": 70,
               "judgment": "Fine."}]
    check("rows with no unjudged key count as judged",
          grade_guard.judged_share(legacy) == 1.0)


# ─── (a) the sentence and the number are one object ─────────────────────────

def test_summary_matches_score():
    print("\n(a) the headline is the score, said in words")
    rows = [{"criteria": "A", "percentage": 85, "maxScore": 25},
            {"criteria": "B", "percentage": 50, "maxScore": 25},
            {"criteria": "C", "percentage": 10, "maxScore": 25},
            {"criteria": "D", "percentage": 0, "maxScore": 25,
             "unjudged": True}]
    tally = scoring_service.requirement_tally(rows)
    check("met/partly/missed counted", tally == {"met": 1, "partly": 1,
                                                 "missed": 1, "total": 3}, tally)

    line = scoring_service.build_summary(4.5, 10, requirements=rows)
    check("headline carries the score", "4.5 out of 10" in line, line)
    check("headline carries the same rows", "1 of the 3 things" in line, line)
    check("partial work is named", "started but not finished" in line, line)

    # THE DEFECT: two submissions, same coverage sentence, different marks.
    zero = [{"criteria": "A", "percentage": 0, "maxScore": 50},
            {"criteria": "B", "percentage": 0, "maxScore": 50}]
    good = [{"criteria": "A", "percentage": 90, "maxScore": 50},
            {"criteria": "B", "percentage": 90, "maxScore": 50}]
    s_zero = scoring_service.build_summary(0, 10, requirements=zero)
    s_good = scoring_service.build_summary(9, 10, requirements=good)
    check("a zero cannot claim coverage", "fully did 0 of the 2" in s_zero, s_zero)
    check("a high mark says so", "fully did 2 of the 2" in s_good, s_good)
    check("the two can never read alike", s_zero != s_good)

    check("old concept call still works",
          "covered 3 of the 7" in scoring_service.build_summary(2, 10, 3, 7))
    check("no requirements, no clause",
          scoring_service.build_summary(2, 10, requirements=[]) ==
          "You scored 2 out of 10.")


def test_grammar():
    print("\n(a) the sentence reads properly")
    one = [{"criteria": "A", "percentage": 90, "maxScore": 100}]
    check("singular 'thing'", "1 of the 1 thing this task asked for"
          in scoring_service.build_summary(10, 10, requirements=one))
    partial_one = [{"criteria": "A", "percentage": 90, "maxScore": 50},
                   {"criteria": "B", "percentage": 50, "maxScore": 50}]
    check("singular 'was'", "1 more was started"
          in scoring_service.build_summary(7, 10, requirements=partial_one))


if __name__ == "__main__":
    for fn in (test_nontext, test_artefact_not_capped,
               test_no_length_penalty_on_artefacts, test_unjudged_not_zero,
               test_minority_judged_refused, test_summary_matches_score,
               test_grammar):
        fn()
    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'ALL PASS'}")
    sys.exit(1 if FAILURES else 0)
