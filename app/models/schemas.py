# app/models/schemas.py
# All request and response shapes

from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# ===== REQUEST MODELS =====

class SubmitAnswerRequest(BaseModel):
    caseStudyId: int
    studentId: int
    answerText: str


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
