# tests/test_candidate_answer_bank.py
import pytest
from django.utils import timezone
from django.contrib.auth.models import User
from openoutreach.career.models import CandidateProfile, JobApplicationAnswer, CandidateApplicationProfile
from openoutreach.career.answer_bank import (
    normalize_question,
    is_sensitive_question,
    get_answer_for_question,
    save_answer,
)


@pytest.mark.django_db
class TestCandidateAnswerBank:
    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.user = User.objects.create_user(username="testcand", email="cand@example.com")
        self.profile = CandidateProfile.objects.create(
            user=self.user,
            full_name="Alice Smith",
            email="cand@example.com"
        )
        self.app_profile = CandidateApplicationProfile.objects.create(
            candidate_profile=self.profile,
            full_name="Alice Smith",
            email="cand@example.com",
            phone="+15550199",
            confirmed=True
        )

    def test_question_normalization(self):
        q1 = "Are you legally authorized to work in the US?"
        q2 = "Do you currently have US work authorization?"
        q3 = "Are you eligible to work in the United States?"
        assert normalize_question(q1) == "work_authorization_US"
        assert normalize_question(q2) == "work_authorization_US"
        assert normalize_question(q3) == "work_authorization_US"

    def test_sensitive_question_default(self):
        assert is_sensitive_question("What is your race?") is True
        assert is_sensitive_question("Please specify your gender") is True

        status, val, key = get_answer_for_question(self.profile, "What is your ethnicity?")
        assert status == "OPTIONAL_SKIP"
        assert val == "Decline to self-identify"

    def test_answer_precedence(self):
        # 1. Save global answer
        save_answer(self.profile, "work_authorization_US", True, answer_type="boolean")

        status, val, key = get_answer_for_question(self.profile, "Are you authorized to work in the US?")
        assert status == "ANSWER_FOUND"
        assert val is True

    def test_unknown_question_requires_user(self):
        status, val, key = get_answer_for_question(self.profile, "What is your favorite color?")
        assert status == "UNKNOWN_REQUIRES_USER"
        assert val is None
