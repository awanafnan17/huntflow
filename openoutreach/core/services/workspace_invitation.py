import hmac
import hashlib
import secrets
from typing import Optional, Tuple
from django.conf import settings
from django.utils import timezone
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError

from openoutreach.core.models import (
    Workspace,
    WorkspaceMembership,
    WorkspaceInvitation,
    PasswordResetToken,
    EmailVerificationToken,
)
from openoutreach.core.services.entitlements import check_entitlement
from openoutreach.core.services.audit_service import record_audit_event


def hash_token(raw_token: str, version: str = "v1") -> str:
    """Compute a peppered HMAC SHA-256 hash of a raw token."""
    pepper_str = getattr(settings, "TOKEN_PEPPER", "")
    if not pepper_str:
        pepper_str = getattr(settings, "SECRET_KEY", "openoutreach_server_pepper")
    pepper = pepper_str.encode("utf-8")
    return hmac.new(pepper, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


def create_invitation(workspace: Workspace, email: str, role: str = "campaign_manager", invited_by: Optional[User] = None) -> Tuple[WorkspaceInvitation, str]:
    """Create a workspace invitation after checking member entitlement limit. Returns (invitation, raw_token)."""
    check_entitlement(workspace, "members", requested_quantity=1)

    existing_invite = WorkspaceInvitation.objects.filter(workspace=workspace, email=email, status="pending").first()
    if existing_invite:
        if existing_invite.expires_at <= timezone.now():
            existing_invite.status = "expired"
            existing_invite.save()

    raw_token = secrets.token_urlsafe(32)
    tok_hash = hash_token(raw_token)
    expires_at = timezone.now() + timezone.timedelta(days=7)

    invite = WorkspaceInvitation.objects.create(
        workspace=workspace,
        email=email,
        role=role,
        token_hash=tok_hash,
        token_hash_version="v1",
        status="pending",
        invited_by=invited_by,
        expires_at=expires_at,
    )

    record_audit_event(
        workspace=workspace,
        event_type="workspace_invitation_created",
        actor=invited_by,
        target=invite,
        action_key=f"inv_create_{invite.id}",
        metadata={"email": email, "role": role},
    )
    return invite, raw_token


def accept_invitation(raw_token: str, user: User) -> WorkspaceMembership:
    """Accept an active workspace invitation using raw token."""
    tok_hash = hash_token(raw_token)
    invite = WorkspaceInvitation.objects.filter(token_hash=tok_hash, token_hash_version="v1", status="pending").first()
    if not invite:
        raise ValidationError("Invalid or expired invitation token.")

    if invite.expires_at < timezone.now():
        invite.status = "expired"
        invite.save()
        raise ValidationError("Invitation token has expired.")

    if user.email and invite.email.lower() != user.email.lower():
        raise ValidationError("Invitation email does not match authenticated user email.")

    membership, created = WorkspaceMembership.objects.get_or_create(
        workspace=invite.workspace,
        user=user,
        defaults={"role": invite.role, "status": "active"},
    )
    if not created and membership.status != "active":
        membership.status = "active"
        membership.role = invite.role
        membership.save()

    invite.status = "accepted"
    invite.used_at = timezone.now()
    invite.save()

    record_audit_event(
        workspace=invite.workspace,
        event_type="workspace_invitation_accepted",
        actor=user,
        target=membership,
        action_key=f"inv_accept_{invite.id}",
        metadata={"email": invite.email, "role": invite.role},
    )
    return membership


def revoke_invitation(invitation_id: int, workspace: Workspace, actor: User) -> WorkspaceInvitation:
    """Revoke a pending workspace invitation."""
    invite = WorkspaceInvitation.objects.filter(id=invitation_id, workspace=workspace, status="pending").first()
    if not invite:
        raise ValidationError("Pending invitation not found.")

    invite.status = "revoked"
    invite.revoked_at = timezone.now()
    invite.save()

    record_audit_event(
        workspace=workspace,
        event_type="workspace_invitation_revoked",
        actor=actor,
        target=invite,
        action_key=f"inv_revoke_{invite.id}",
        metadata={"email": invite.email},
    )
    return invite


def generate_email_verification_token(user: User, email: str) -> Tuple[EmailVerificationToken, str]:
    """Generate a 24-hour token for email verification. Returns (token_obj, raw_token)."""
    raw_token = secrets.token_urlsafe(32)
    tok_hash = hash_token(raw_token)
    expires_at = timezone.now() + timezone.timedelta(hours=24)

    tok_obj = EmailVerificationToken.objects.create(
        user=user,
        email=email,
        token_hash=tok_hash,
        token_hash_version="v1",
        expires_at=expires_at,
    )

    if user.workspace_memberships.exists():
        ws = user.workspace_memberships.first().workspace
        record_audit_event(
            workspace=ws,
            event_type="email_verification_created",
            actor=user,
            target=user,
            action_key=f"email_ver_create_{tok_obj.id}",
            metadata={"email": email},
        )

    return tok_obj, raw_token


def verify_email_token(raw_token: str) -> EmailVerificationToken:
    """Confirm an email verification token using raw token."""
    tok_hash = hash_token(raw_token)
    tok = EmailVerificationToken.objects.filter(token_hash=tok_hash, token_hash_version="v1", is_verified=False).first()
    if not tok:
        raise ValidationError("Invalid or already used verification token.")

    if tok.expires_at < timezone.now():
        raise ValidationError("Verification token has expired.")

    tok.is_verified = True
    tok.verified_at = timezone.now()
    tok.save()

    if tok.user.workspace_memberships.exists():
        ws = tok.user.workspace_memberships.first().workspace
        record_audit_event(
            workspace=ws,
            event_type="email_verified",
            actor=tok.user,
            target=tok.user,
            action_key=f"email_verified_{tok.id}",
            metadata={"email": tok.email},
        )

    return tok


def generate_password_reset_token(user: User) -> Tuple[PasswordResetToken, str]:
    """Generate a 1-hour token for password reset. Returns (token_obj, raw_token)."""
    raw_token = secrets.token_urlsafe(32)
    tok_hash = hash_token(raw_token)
    expires_at = timezone.now() + timezone.timedelta(hours=1)

    tok_obj = PasswordResetToken.objects.create(
        user=user,
        token_hash=tok_hash,
        token_hash_version="v1",
        expires_at=expires_at,
    )

    if user.workspace_memberships.exists():
        ws = user.workspace_memberships.first().workspace
        record_audit_event(
            workspace=ws,
            event_type="password_reset_requested",
            actor=user,
            target=user,
            action_key=f"pwd_reset_req_{tok_obj.id}",
            metadata={"user_id": user.id},
        )

    return tok_obj, raw_token


def confirm_password_reset(raw_token: str, new_password: str) -> User:
    """Set new password using a valid reset token, enforcing password validation and session invalidation."""
    tok_hash = hash_token(raw_token)
    tok = PasswordResetToken.objects.filter(token_hash=tok_hash, token_hash_version="v1", is_used=False).first()
    if not tok:
        raise ValidationError("Invalid or already used password reset token.")
    if not tok:
        raise ValidationError("Invalid or already used password reset token.")

    if tok.expires_at < timezone.now():
        raise ValidationError("Password reset token has expired.")

    user = tok.user
    validate_password(new_password, user=user)

    user.set_password(new_password)
    user.save()

    # Invalidate all unused password reset tokens for user
    PasswordResetToken.objects.filter(user=user, is_used=False).update(is_used=True, used_at=timezone.now())

    # Invalidate existing user sessions
    for s in Session.objects.all():
        data = s.get_decoded()
        if data.get("_auth_user_id") == str(user.id):
            s.delete()

    if user.workspace_memberships.exists():
        ws = user.workspace_memberships.first().workspace
        record_audit_event(
            workspace=ws,
            event_type="password_reset_completed",
            actor=user,
            target=user,
            action_key=f"pwd_reset_comp_{user.id}_{int(timezone.now().timestamp())}",
            metadata={"user_id": user.id},
        )

    return user
