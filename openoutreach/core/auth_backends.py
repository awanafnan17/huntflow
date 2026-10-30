# openoutreach/core/auth_backends.py
from __future__ import annotations

import logging
from typing import Optional

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.http import HttpRequest

logger = logging.getLogger(__name__)


class EmailOrUsernameModelBackend(ModelBackend):
    """Authentication backend allowing users to log in with either username or email.

    Preserves all existing passwords bit-for-bit with zero rotation/mutation.
    Resolves users via:
    1. Exact or case-insensitive username match
    2. Exact or case-insensitive User.email match
    3. Exact or case-insensitive CandidateProfile.email match

    Safe collision handling: If multiple accounts match, attempts password verification
    and authenticates if exactly one candidate matches the provided password.
    """

    def authenticate(
        self,
        request: Optional[HttpRequest] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        **kwargs
    ):
        if not username or password is None:
            return None

        identifier = username.strip()
        UserModel = get_user_model()

        # 1. Match by username (case-insensitive)
        candidates = list(UserModel.objects.filter(username__iexact=identifier))

        # 2. Match by User.email (case-insensitive)
        if not candidates and "@" in identifier:
            candidates = list(UserModel.objects.filter(email__iexact=identifier))

        # 3. Match by CandidateProfile.email (case-insensitive)
        if not candidates and "@" in identifier:
            try:
                from openoutreach.career.models import CandidateProfile
                cp_user_ids = list(
                    CandidateProfile.objects.filter(email__iexact=identifier)
                    .values_list("user_id", flat=True)
                )
                if cp_user_ids:
                    candidates = list(UserModel.objects.filter(id__in=cp_user_ids))
            except Exception as e:
                logger.debug("CandidateProfile lookup skipped: %s", e)

        if not candidates:
            # Mitigate timing attacks by running the default password hasher once
            UserModel().set_password(password)
            return None

        # Verify password on candidate(s)
        matching_users = []
        for user in candidates:
            if user.check_password(password) and self.user_can_authenticate(user):
                matching_users.append(user)

        if len(matching_users) == 1:
            return matching_users[0]
        elif len(matching_users) > 1:
            # Ambiguous collision: fail safely
            logger.warning(
                "Multiple active accounts matched login identifier '%s'; failing safely.",
                identifier
            )
            return None

        return None
