# tests/test_real_chat_onboarding.py
import pytest
from django.contrib.auth.models import User
from openoutreach.chat.models import AgentConversation, AgentMessage
from openoutreach.career.models import CandidateProfile, CandidateApplicationProfile, JobApplicationAnswer
from openoutreach.chat.service import process_chat_message


@pytest.mark.django_db
class TestRealChatJobOnboarding:
    @pytest.fixture(autouse=True)
    def setup_data(self):
        self.user = User.objects.create_user(username="chatuser", email="chatuser@example.com")
        self.cand_prof = CandidateProfile.objects.create(
            user=self.user,
            full_name="Chat User",
            email="chatuser@example.com"
        )
        self.conv = AgentConversation.objects.create(user=self.user)

    def test_ask_once_chat_workflow_and_reuse(self):
        """Test full Ask-Once chat onboarding sequence and reuse across subsequent campaigns."""
        # 1. First campaign request triggers onboarding sequence
        reply1 = process_chat_message(self.user, self.conv, "Find software jobs and apply for me")
        assert "Before I start, I need a few details" in reply1
        assert "What phone number" in reply1
        assert self.conv.onboarding_state == "JOB_ONBOARDING_IN_PROGRESS"

        # 2. User answers phone
        reply2 = process_chat_message(self.user, self.conv, "+1 555-0199")
        assert "What is your current city and country?" in reply2
        ans_phone = JobApplicationAnswer.objects.get(candidate_profile=self.cand_prof, canonical_key="phone_number")
        assert ans_phone.answer_value == "+1 555-0199"

        # 3. User answers city/country
        reply3 = process_chat_message(self.user, self.conv, "Seattle, US")
        assert "Where are you legally authorized to work?" in reply3

        # 4. User answers work auth
        reply4 = process_chat_message(self.user, self.conv, "US, CA")
        assert "Will you require visa sponsorship" in reply4

        # 5. User answers visa sponsorship
        reply5 = process_chat_message(self.user, self.conv, "No")
        assert "Are you willing to relocate" in reply5

        # 6. User answers relocation
        reply6 = process_chat_message(self.user, self.conv, "Yes")
        assert "Which work modes are acceptable?" in reply6

        # 7. User answers work modes
        reply7 = process_chat_message(self.user, self.conv, "Remote, Hybrid")
        assert "What job titles or roles" in reply7

        # 8. User answers target roles
        reply8 = process_chat_message(self.user, self.conv, "Software Engineer, Python Developer")
        assert "Which employment types" in reply8

        # 9. User answers employment types
        reply9 = process_chat_message(self.user, self.conv, "Full-time")
        assert "What is your earliest start date?" in reply9

        # 10. User answers start date
        reply10 = process_chat_message(self.user, self.conv, "Immediately")
        assert "What is your notice period?" in reply10

        # 11. User answers notice period
        reply11 = process_chat_message(self.user, self.conv, "2 weeks")
        assert "What is your expected salary range" in reply11

        # 12. User answers salary
        reply12 = process_chat_message(self.user, self.conv, "$120,000")
        assert "Job Application Profile Summary" in reply12
        assert self.conv.onboarding_state == "JOB_ONBOARDING_REVIEW"

        # 13. Confirm profile
        reply13 = process_chat_message(self.user, self.conv, "Use automatically")
        assert "Job Application Campaign Active" in reply13
        assert self.conv.onboarding_state == "JOB_ONBOARDING_CONFIRMED"

        app_prof = CandidateApplicationProfile.objects.get(candidate_profile=self.cand_prof)
        assert app_prof.confirmed is True

        # 14. Second campaign request for new jobs does NOT repeat questions
        conv2 = AgentConversation.objects.create(user=self.user)
        reply_second = process_chat_message(self.user, conv2, "Find remote python jobs and apply")
        assert "Job Application Campaign Active" in reply_second
        assert "Before I start" not in reply_second
