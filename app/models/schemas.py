# app/models/schemas.py
# All request and response shapes
#
# CHANGED: SubmitAnswerRequest now accepts fileUrl + fileName.
# Previously, the frontend POSTed these fields but Pydantic silently
# dropped them — so case-study file uploads never made it into the DB.

from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# ===== REQUEST MODELS =====

class SubmitAnswerRequest(BaseModel):
    caseStudyId: int
    studentId: int
    answerText: str = ""
    fileUrl: Optional[str] = None
    fileName: Optional[str] = None
    fileData: Optional[str] = None   # base64 file bytes — storage-free upload path


class TestReviewRequest(BaseModel):
    studentAnswer: str
    caseStudy: dict       # {title, description, questions}
    modelAnswer: str | dict
    gradingRubric: dict   # {criteria: [{name, maxScore, weight}]}
    keyConcepts: list[str]
    wordLimitMin: int = 300
    wordLimitMax: int = 500


class MentorApproveRequest(BaseModel):
    mentorId: int
    mentorScore: Optional[float] = None
    mentorFeedback: Optional[str] = None


# ===== RESPONSE MODELS =====

class RubricScore(BaseModel):
    criteria: str
    maxScore: int
    score: float
    percentage: int
    status: str


class StudentFeedback(BaseModel):
    score: float
    grade: str
    summary: str
    rubricScores: list[RubricScore]
    strengths: list[str]
    improvements: list[str]
    missingConcepts: list[str]
    coveredConcepts: list[str]
    suggestions: list[str]
    detailedFeedback: str
    wordCount: int
    wordCountMessage: str
    encouragement: str


class MentorSummary(BaseModel):
    score: float
    grade: str
    needsAttention: bool
    plagiarismRisk: str
    quickAction: str
    keyMissing: list[str]
    mentorAlert: bool
    mentorAlertReason: str
    conceptCoverage: str


class ReviewResponse(BaseModel):
    success: bool
    submission: dict
    feedback: StudentFeedback
    mentorReport: MentorSummary
    processingTimeMs: int