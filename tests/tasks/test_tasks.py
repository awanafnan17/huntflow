# tests/tasks/test_tasks.py
import pytest
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone

from openoutreach.crm.models import Deal
from openoutreach.core.agents.follow_up import FollowUpDecision
from openoutreach.core.db.deals import set_profile_state
from openoutreach.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from openoutreach.core.models import Task
from openoutreach.linkedin.models import ActionLog
from openoutreach.linkedin.ml.qualifier import BayesianQualifier
from linkedin_cli.enums import ProfileState
from linkedin_cli.exceptions import SkipProfile, ReachedConnectionLimit
from openoutreach.linkedin.tasks.connect import ConnectStrategy, handle_connect
from openoutreach.linkedin.tasks.check_pending import handle_check_pending
from openoutreach.linkedin.tasks.follow_up import handle_follow_up


SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
}


def _mock_strategy(candidate, qualifier=None):
    """Build a ConnectStrategy that returns a fixed candidate."""
    return ConnectStrategy(
        find_candidate=lambda s: candidate,
        pre_connect=None,
        qualifier=qualifier or MagicMock(explain=lambda *a, **kw: ""),
    )


def _assert_deal_state(session, public_id, expected_state: ProfileState):
    from openoutreach.crm.models import Deal
    deal = Deal.objects.get(
        lead__linkedin_url=f"https://www.linkedin.com/in/{public_id}/",
        campaign=session.campaign,
    )
    assert deal.state == expected_state


def _make_qualified(session, public_id="alice"):
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(session, url, SAMPLE_PROFILE)
    promote_lead_to_deal(session, public_id)


def _make_pending_due(session, public_id="alice"):
    """Create a PENDING deal whose ``next_check_pending_at`` is overdue."""
    _make_qualified(session, public_id)
    set_profile_state(session, public_id, ProfileState.PENDING.value)
    from openoutreach.crm.models import Deal
    Deal.objects.filter(lead__public_identifier=public_id).update(
        next_check_pending_at=timezone.now() - timedelta(minutes=1),
    )
    Task.objects.all().delete()


def _make_connected(session, public_id="alice"):
    _make_qualified(session, public_id)
    set_profile_state(session, public_id, ProfileState.CONNECTED.value)
    from openoutreach.crm.models import Deal
    from openoutreach.linkedin.models import ProviderRelationship
    deal = Deal.objects.filter(lead__public_identifier=public_id, campaign=session.campaign).first()
    if deal:
        prof = getattr(session, "linkedin_profile", None) or getattr(getattr(session, "campaign", None), "linkedin_profile", None)
        if not prof and getattr(session, "campaign", None) and session.campaign.workspace:
            from openoutreach.linkedin.models import LinkedInProfile
            prof = LinkedInProfile.objects.filter(workspace=session.campaign.workspace).first()
            if not prof:
                prof = LinkedInProfile.objects.create(
                    workspace=session.campaign.workspace,
                    session_status="valid",
                    active=True,
                )
            if hasattr(session, "linkedin_profile"):
                session.linkedin_profile = prof
        ProviderRelationship.objects.update_or_create(
            workspace=session.campaign.workspace,
            linkedin_profile=prof,
            lead=deal.lead,
            defaults={
                "deal": deal,
                "classification": "CONNECTED",
                "evidence_sufficient": True,
                "last_observed_at": timezone.now(),
            },
        )
    Task.objects.all().delete()


def _make_task(task_type, payload, **kwargs):
    """Create a lazy task and mark it RUNNING (matching daemon behavior)."""
    return Task.objects.create(
        task_type=task_type,
        status=Task.Status.RUNNING,
        scheduled_at=kwargs.pop("scheduled_at", timezone.now()),
        started_at=timezone.now(),
        payload=payload,
        **kwargs,
    )


def _build_context(fake_session):
    """Build qualifiers dict for task handlers."""
    qualifier = BayesianQualifier(seed=42)
    qualifier.rank_profiles = lambda profiles, **kw: profiles
    return {fake_session.campaign.pk: qualifier}


# Mock path for the new note-aware sender (Batch 2)
_SENDER_MOCK = "openoutreach.linkedin.browser.connect_with_note.send_connection_request_with_note"

# ── handle_connect tests ────────────────────────────────────────


@pytest.mark.django_db
class TestHandleConnect:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    def _candidate(self):
        return {"public_identifier": "alice", "url": "https://www.linkedin.com/in/alice/", "profile": SAMPLE_PROFILE}

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch(_SENDER_MOCK)
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_sends_connection_and_records(self, mock_status, mock_send, mock_visit, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.return_value = ProfileState.PENDING

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        _assert_deal_state(fake_session, "alice", ProfileState.PENDING)
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.CONNECT).count() == 1

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch(_SENDER_MOCK)
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_pending_stamps_next_check(self, mock_status, mock_send, mock_visit, mock_strategy, fake_session):
        """Connect → PENDING: state hook should stamp next_check_pending_at, no follow-up Task."""
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.return_value = ProfileState.PENDING

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        deal = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        assert deal.next_check_pending_at is not None
        assert not Task.objects.filter(task_type=Task.TaskType.CHECK_PENDING).exists()

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_marks_preexisting_connected(self, mock_status, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.CONNECTED

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        _assert_deal_state(fake_session, "alice", ProfileState.CONNECTED)
        # Lazy model: no follow_up Task is created on CONNECTED state entry.
        assert not Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).exists()

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_handles_rate_limit(self, mock_status, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.side_effect = ReachedConnectionLimit("weekly limit")

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        assert ActionLog.ActionType.CONNECT in fake_session.linkedin_profile._exhausted

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch(_SENDER_MOCK)
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_handles_skip_profile(self, mock_status, mock_send, mock_visit, mock_strategy, fake_session):
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.side_effect = SkipProfile("bad profile")

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        _assert_deal_state(fake_session, "alice", ProfileState.FAILED)

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    def test_skips_when_no_candidate(self, mock_strategy, fake_session):
        """Lazy: handler marks the slot done; planner re-plans next window."""
        mock_strategy.return_value = _mock_strategy(None)

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        # No new tasks created by the handler.
        assert Task.objects.exclude(pk=task.pk).count() == 0

    def test_skips_when_rate_limited(self, fake_session):
        fake_session.linkedin_profile.connect_daily_limit = 1
        fake_session.linkedin_profile.save(update_fields=["connect_daily_limit"])
        ActionLog.objects.create(
            linkedin_profile=fake_session.linkedin_profile,
            campaign=fake_session.campaign,
            action_type=ActionLog.ActionType.CONNECT,
        )

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        assert Task.objects.exclude(pk=task.pk).count() == 0

    # ── Batch 2: Connection note wiring tests ────────────────────

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch(_SENDER_MOCK)
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_note_passed_to_sender(self, mock_status, mock_send, mock_visit, mock_strategy, fake_session):
        """Verify the sender receives a non-empty note when notes are enabled."""
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.return_value = ProfileState.PENDING

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        # The sender must have been called with a note= kwarg
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        note_arg = call_kwargs.kwargs.get("note") or call_kwargs[1].get("note", "")
        assert len(note_arg) > 0, "Note must be non-empty when notes are enabled"
        assert len(note_arg) <= 300, "Note must not exceed LinkedIn 300-char limit"

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch(_SENDER_MOCK)
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_actionlog_metadata_saved(self, mock_status, mock_send, mock_visit, mock_strategy, fake_session):
        """Verify ActionLog metadata contains connection note audit trail."""
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.return_value = ProfileState.PENDING

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        log = ActionLog.objects.filter(action_type=ActionLog.ActionType.CONNECT).first()
        assert log is not None
        assert log.metadata is not None
        assert "note" in log.metadata
        assert "note_source" in log.metadata
        assert log.metadata["note_source"] in ("template", "llm")
        assert "note_length" in log.metadata
        assert log.metadata["note_length"] > 0
        assert log.metadata["note_enabled"] is True
        assert log.metadata["note_was_sent"] is True

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch(_SENDER_MOCK)
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_note_disabled_sends_empty(self, mock_status, mock_send, mock_visit, mock_strategy, fake_session):
        """When connection_note_enabled=False, sender receives empty note."""
        fake_session.campaign.connection_note_enabled = False
        fake_session.campaign.save()
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_send.return_value = ProfileState.PENDING

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        call_kwargs = mock_send.call_args
        note_arg = call_kwargs.kwargs.get("note") or call_kwargs[1].get("note", "")
        assert note_arg == "", "Note must be empty when disabled"

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch(_SENDER_MOCK)
    @patch("linkedin_cli.actions.status.get_connection_status")
    @patch("linkedin_cli.actions.connect.send_connection_request")
    def test_old_sender_not_called_directly(self, mock_old_send, mock_status, mock_new_send, mock_visit, mock_strategy, fake_session):
        """Prove the OLD send_connection_request is never called directly from handle_connect."""
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_new_send.return_value = ProfileState.PENDING

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})
        handle_connect(task, fake_session, _build_context(fake_session))

        mock_old_send.assert_not_called()

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch(_SENDER_MOCK)
    @patch("linkedin_cli.actions.status.get_connection_status")
    @patch("linkedin_cli.actions.connect.send_connection_request")
    def test_note_enabled_ui_failure_does_not_call_old_sender(self, mock_old_send, mock_status, mock_new_send, mock_visit, mock_strategy, fake_session):
        """When connection_note_enabled=True and the wrapper raises ConnectionNoteUIError, the old sender is NOT called."""
        from openoutreach.linkedin.browser.connect_with_note import ConnectionNoteUIError
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_new_send.side_effect = ConnectionNoteUIError("Textarea missing")

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})

        with pytest.raises(ConnectionNoteUIError):
            handle_connect(task, fake_session, _build_context(fake_session))

        mock_old_send.assert_not_called()

    @patch("openoutreach.linkedin.tasks.connect.strategy_for")
    @patch("linkedin_cli.actions.search.visit_profile")
    @patch(_SENDER_MOCK)
    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_note_enabled_ui_failure_actionlog_metadata(self, mock_status, mock_new_send, mock_visit, mock_strategy, fake_session):
        """When the wrapper raises ConnectionNoteUIError, ActionLog metadata registers note_was_sent=False."""
        from openoutreach.linkedin.browser.connect_with_note import ConnectionNoteUIError
        _make_qualified(fake_session)
        mock_strategy.return_value = _mock_strategy(self._candidate())
        mock_status.return_value = ProfileState.QUALIFIED
        mock_new_send.side_effect = ConnectionNoteUIError("Add a note button missing")

        task = _make_task(Task.TaskType.CONNECT, {"campaign_id": fake_session.campaign.pk})

        with pytest.raises(ConnectionNoteUIError):
            handle_connect(task, fake_session, _build_context(fake_session))

        log = ActionLog.objects.filter(action_type=ActionLog.ActionType.CONNECT).first()
        assert log is not None
        assert log.metadata is not None
        assert log.metadata["note_was_sent"] is False
        assert log.metadata["note_required"] is True
        assert "failure_reason" in log.metadata
        assert "Add a note button missing" in log.metadata["failure_reason"]

    @patch("linkedin_cli.actions.connect.send_connection_request")
    def test_wrapper_disabled_delegates_to_original(self, mock_old_send, fake_session):
        """When note is empty, wrapper delegates directly to the original send_connection_request."""
        from openoutreach.linkedin.browser.connect_with_note import send_connection_request_with_note
        mock_old_send.return_value = ProfileState.PENDING

        profile = {"public_identifier": "alice"}
        send_connection_request_with_note(fake_session, profile, note="")

        mock_old_send.assert_called_once_with(session=fake_session, profile=profile)



# ── handle_check_pending tests ──────────────────────────────────────


@pytest.mark.django_db
class TestHandleCheckPending:
    @pytest.fixture(autouse=True)
    def _db(self, db):
        pass

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_transitions_due_deal_to_connected(self, mock_status, fake_session):
        mock_status.return_value = ProfileState.CONNECTED
        _make_pending_due(fake_session)

        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})
        handle_check_pending(task, fake_session, _build_context(fake_session))

        _assert_deal_state(fake_session, "alice", ProfileState.CONNECTED)
        # Lazy model: no follow_up task is auto-enqueued on the transition.
        assert not Task.objects.filter(task_type=Task.TaskType.FOLLOW_UP).exists()

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_stays_pending_doubles_backoff_and_restamps(self, mock_status, fake_session):
        mock_status.return_value = ProfileState.PENDING
        _make_pending_due(fake_session)
        from linkedin_cli.url_utils import public_id_to_url
        Deal.objects.filter(lead__linkedin_url=public_id_to_url("alice")).update(backoff_hours=72)

        before = timezone.now()
        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})
        handle_check_pending(task, fake_session, _build_context(fake_session))

        deal = Deal.objects.get(lead__linkedin_url=public_id_to_url("alice"))
        assert deal.backoff_hours == 144  # 72 × 2
        # next_check_pending_at re-stamped by the state hook to now + 144h
        expected = before + timedelta(hours=144)
        assert abs((deal.next_check_pending_at - expected).total_seconds()) < 10

    def test_skips_when_no_due_deals(self, fake_session):
        # No PENDING deals at all → handler marks slot done.
        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})
        handle_check_pending(task, fake_session, _build_context(fake_session))
        # No exceptions; no new tasks created.
        assert Task.objects.exclude(pk=task.pk).count() == 0

    @patch("linkedin_cli.actions.status.get_connection_status")
    def test_ignores_not_yet_due_pending(self, mock_status, fake_session):
        """A PENDING deal whose next_check is in the future should not be picked."""
        _make_qualified(fake_session, "alice")
        set_profile_state(fake_session, "alice", ProfileState.PENDING.value)
        Deal.objects.filter(lead__public_identifier="alice").update(
            next_check_pending_at=timezone.now() + timedelta(hours=10),
        )
        Task.objects.all().delete()

        task = _make_task(Task.TaskType.CHECK_PENDING, {"campaign_id": fake_session.campaign.pk})
        handle_check_pending(task, fake_session, _build_context(fake_session))
        mock_status.assert_not_called()


# ── handle_follow_up tests ─────────────────────────────────────


@pytest.mark.django_db
class TestHandleFollowUp:
    @patch("openoutreach.linkedin.db.chat.sync_conversation")
    @patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing")
    @patch("linkedin_cli.actions.message.send_raw_message", return_value=True)
    @patch("openoutreach.core.agents.follow_up.run_follow_up_agent")
    def test_send_message_records_action(self, mock_agent, mock_send, mock_materialize, mock_sync, fake_session):
        mock_agent.return_value = FollowUpDecision(
            action="send_message", message="hey Alice, saw what you guys are building... curious what you're focused on rn?", follow_up_hours=72,
        )
        _make_connected(fake_session)

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        # Lazy: agent gets the resolved Deal.
        mock_materialize.assert_called_once()
        materialized_deal = mock_materialize.call_args[0][0]
        assert materialized_deal.lead.public_identifier == "alice"

        mock_agent.assert_called_once()
        agent_deal = mock_agent.call_args[0][1]
        assert agent_deal.lead.public_identifier == "alice"

        mock_send.assert_called_once()
        mock_sync.assert_called_once_with(fake_session, "alice")
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 1

    @patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing")
    @patch("linkedin_cli.actions.message.send_raw_message", return_value=False)
    @patch("openoutreach.core.agents.follow_up.run_follow_up_agent")
    def test_send_failure_preserves_connected_state_on_cooldown(self, mock_agent, mock_send, mock_materialize, fake_session):
        mock_agent.return_value = FollowUpDecision(
            action="send_message", message="Hi!", follow_up_hours=24,
        )
        _make_connected(fake_session)

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 0
        deal = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        assert deal.state == ProfileState.CONNECTED

    @patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing")
    @patch("openoutreach.core.agents.follow_up.run_follow_up_agent")
    def test_mark_completed_sets_state(self, mock_agent, mock_materialize, fake_session):
        mock_agent.return_value = FollowUpDecision(
            action="mark_completed", outcome="unresponsive", follow_up_hours=0,
        )
        _make_connected(fake_session)

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 0
        deal = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        assert deal.state == ProfileState.COMPLETED
        assert deal.outcome == "unresponsive"

    @patch("openoutreach.core.db.summaries.materialize_profile_summary_if_missing")
    @patch("openoutreach.core.agents.follow_up.run_follow_up_agent")
    def test_wait_bumps_update_date(self, mock_agent, mock_materialize, fake_session):
        mock_agent.return_value = FollowUpDecision(action="wait", follow_up_hours=48)
        _make_connected(fake_session)
        deal_before = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        original_update = deal_before.update_date

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        deal_after = Deal.objects.get(lead__public_identifier="alice", campaign=fake_session.campaign)
        assert deal_after.update_date > original_update

    @patch("openoutreach.core.agents.follow_up.run_follow_up_agent")
    def test_skips_when_no_eligible_deal(self, mock_agent, fake_session):
        """No CONNECTED deals → handler marks slot done without calling the agent."""
        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))
        mock_agent.assert_not_called()

    def test_skips_on_rate_limit(self, fake_session):
        _make_connected(fake_session)
        fake_session.linkedin_profile.follow_up_daily_limit = 1
        fake_session.linkedin_profile.save(update_fields=["follow_up_daily_limit"])
        ActionLog.objects.create(
            linkedin_profile=fake_session.linkedin_profile,
            campaign=fake_session.campaign,
            action_type=ActionLog.ActionType.FOLLOW_UP,
        )

        task = _make_task(Task.TaskType.FOLLOW_UP, {"campaign_id": fake_session.campaign.pk})
        handle_follow_up(task, fake_session, _build_context(fake_session))

        # No follow-up was actually performed (no ActionLog row).
        assert ActionLog.objects.filter(action_type=ActionLog.ActionType.FOLLOW_UP).count() == 1
