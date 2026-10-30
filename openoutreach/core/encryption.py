# openoutreach/core/encryption.py
"""Authenticated AES-GCM encryption at rest for sensitive secrets (LinkedIn session cookies, API tokens, passwords)."""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, Optional

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

# Versioned envelope prefix for AES-GCM
_GCM_V1_PREFIX = "v1::"
_LEGACY_FERNET_PREFIX = "gcm_enc::"


class CredentialEncryptionError(Exception):
    """Base exception for credential encryption/decryption failures."""
    pass


class TamperedCiphertextError(CredentialEncryptionError):
    """Raised when ciphertext or authentication tag has been tampered with."""
    pass


class InvalidKeyError(CredentialEncryptionError):
    """Raised when encryption key is invalid or fails authentication."""
    pass


def _get_raw_key_material(is_production: Optional[bool] = None) -> bytes:
    """
    Safely retrieves or derives a 32-byte (256-bit) AES-GCM encryption key.
    Source priority:
    1. Dedicated environment variable: HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY
    2. Fallback environment variable: HUNTFLOW_ENCRYPTION_KEY
    3. Django settings attribute: HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY / HUNTFLOW_ENCRYPTION_KEY
    4. Derived from Django SECRET_KEY (development/testing environments only)

    In production (DEBUG=False, non-test), requires a dedicated encryption key.
    """
    # 1 & 2. Environment variables
    env_key = os.environ.get("HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY", "").strip() or os.environ.get("HUNTFLOW_ENCRYPTION_KEY", "").strip()
    if env_key:
        try:
            # Check if already a base64 encoded 32-byte key
            raw = base64.urlsafe_b64decode(env_key.encode("utf-8"))
            if len(raw) == 32:
                return raw
        except Exception:
            pass
        import hashlib
        return hashlib.sha256(env_key.encode("utf-8")).digest()

    # 3. Settings attributes
    settings_key = getattr(settings, "HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY", "") or getattr(settings, "HUNTFLOW_ENCRYPTION_KEY", "")
    if settings_key:
        try:
            raw = base64.urlsafe_b64decode(str(settings_key).encode("utf-8"))
            if len(raw) == 32:
                return raw
        except Exception:
            pass
        import hashlib
        return hashlib.sha256(str(settings_key).encode("utf-8")).digest()

    # 4. Dev / Test fallback
    if is_production is None:
        is_testing = bool(
            getattr(settings, "IS_TEST_ENV", False)
            or getattr(settings, "TESTING", False)
            or os.environ.get("PYTEST_CURRENT_TEST")
        )
        is_prod = not getattr(settings, "DEBUG", True) and not is_testing
    else:
        is_prod = is_production

    if is_prod:
        raise ImproperlyConfigured(
            "Production security requirement: Set HUNTFLOW_CREDENTIAL_ENCRYPTION_KEY environment variable for authenticated encryption at rest."
        )

    secret = getattr(settings, "SECRET_KEY", "openoutreach-local-dev-key-change-in-production")
    import hashlib
    return hashlib.sha256(secret.encode("utf-8")).digest()


# Backward compatibility alias
_get_fernet_key = _get_raw_key_material


def encrypt_string(plaintext: str, key_id: str = "primary", custom_key: Optional[bytes] = None) -> str:
    """
    Encrypts a plaintext string using AES-256-GCM authenticated encryption.
    Generates a cryptographically random 12-byte nonce for every encryption call.
    Returns a versioned envelope: v1::<key_id>::<b64_nonce>::<b64_ciphertext_and_tag>
    """
    if not plaintext:
        return ""
    # Avoid double encryption if already enveloped
    if plaintext.startswith(_GCM_V1_PREFIX) or plaintext.startswith(_LEGACY_FERNET_PREFIX):
        return plaintext

    key = custom_key or _get_raw_key_material()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit unique nonce per encryption
    ciphertext_and_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=f"key_id:{key_id}".encode("utf-8"))

    b64_nonce = base64.urlsafe_b64encode(nonce).decode("utf-8")
    b64_ct = base64.urlsafe_b64encode(ciphertext_and_tag).decode("utf-8")
    return f"{_GCM_V1_PREFIX}{key_id}::{b64_nonce}::{b64_ct}"


def decrypt_string(ciphertext: str, custom_key: Optional[bytes] = None) -> str:
    """
    Decrypts a versioned AES-256-GCM ciphertext envelope or legacy Fernet token.
    Fails closed on corrupted ciphertext, wrong key, or tampered authentication tag.
    """
    if not ciphertext:
        return ""

    # Unwrap outer JSON quotes if double-serialized into a JSONField
    if isinstance(ciphertext, str) and ciphertext.startswith('"') and ciphertext.endswith('"') and len(ciphertext) > 1:
        try:
            unwrapped = json.loads(ciphertext)
            if isinstance(unwrapped, str):
                ciphertext = unwrapped
        except Exception:
            pass

    # 1. AES-GCM versioned envelope: v1::<key_id>::<b64_nonce>::<b64_ct>
    if ciphertext.startswith(_GCM_V1_PREFIX):
        parts = ciphertext.split("::")
        if len(parts) != 4:
            logger.error("Tampered or corrupted envelope format: invalid segment count.")
            return ""
        _, key_id, b64_nonce, b64_ct = parts
        try:
            nonce = base64.urlsafe_b64decode(b64_nonce.encode("utf-8"))
            ct_and_tag = base64.urlsafe_b64decode(b64_ct.encode("utf-8"))
            key = custom_key or _get_raw_key_material()
            aesgcm = AESGCM(key)
            decrypted = aesgcm.decrypt(nonce, ct_and_tag, associated_data=f"key_id:{key_id}".encode("utf-8"))
            return decrypted.decode("utf-8")
        except InvalidTag:
            logger.error("Authentication tag verification failed: tampered ciphertext or wrong key.")
            return ""
        except Exception as e:
            logger.error("Decryption failed for v1 envelope: %s", e)
            return ""

    # 2. Legacy Fernet token: gcm_enc::<token>
    if ciphertext.startswith(_LEGACY_FERNET_PREFIX):
        raw_token = ciphertext[len(_LEGACY_FERNET_PREFIX):]
        # If legacy token was itself a v1 envelope
        if raw_token.startswith(_GCM_V1_PREFIX):
            return decrypt_string(raw_token, custom_key=custom_key)
        try:
            key_mat = custom_key or _get_raw_key_material()
            fernet_key = base64.urlsafe_b64encode(key_mat)
            f = Fernet(fernet_key)
            decrypted = f.decrypt(raw_token.encode("utf-8")).decode("utf-8")
            return decrypted
        except InvalidToken:
            logger.error("Failed to decrypt legacy token: invalid token or corrupted key.")
            return ""
        except Exception as e:
            logger.error("Decryption failed for legacy token: %s", e)
            return ""

    # Unencrypted string fallback for backward compatibility
    return ciphertext


def encrypt_dict(data: Optional[Dict[str, Any]], key_id: str = "primary", custom_key: Optional[bytes] = None) -> Optional[str]:
    """Serializes and encrypts a dictionary (e.g. cookie_data storage_state) using AES-256-GCM."""
    if data is None:
        return None
    raw_json = json.dumps(data, sort_keys=True)
    return encrypt_string(raw_json, key_id=key_id, custom_key=custom_key)


def decrypt_dict(encrypted_data: Any, custom_key: Optional[bytes] = None) -> Optional[Dict[str, Any]]:
    """Decrypts and deserializes a stored dictionary. Returns dict directly if already deserialized."""
    if encrypted_data is None:
        return None
    if isinstance(encrypted_data, dict):
        return encrypted_data
    if isinstance(encrypted_data, str):
        if not encrypted_data:
            return None
        decrypted_json = decrypt_string(encrypted_data, custom_key=custom_key)
        if not decrypted_json:
            return None
        try:
            return json.loads(decrypted_json)
        except json.JSONDecodeError:
            logger.error("Failed to parse decrypted JSON payload.")
            return None
    return None
