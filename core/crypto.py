"""Symmetric encryption for secrets stored at rest (e.g. OAuth tokens).

Keys come from ``settings.FIELD_ENCRYPTION_KEYS``: a list of Fernet keys, newest first. New values are encrypted
with the first key; any listed key can decrypt, so keys can be rotated by prepending a new one, running
``manage.py rotate_encryption_keys``, then removing the old key.
"""

import functools

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver


class DecryptionError(ValueError):
    pass


@functools.cache
def _fernet():
    keys = settings.FIELD_ENCRYPTION_KEYS
    if not keys:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEYS must contain at least one Fernet key.")
    try:
        return MultiFernet([Fernet(key) for key in keys])
    except ValueError as exc:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEYS contains an invalid Fernet key.") from exc


@receiver(setting_changed)
def _reset_on_settings_change(setting, **kwargs):
    if setting == "FIELD_ENCRYPTION_KEYS":
        _fernet.cache_clear()


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # Never include the value itself in the message.
        raise DecryptionError("Could not decrypt value: wrong key or corrupted data.") from None
