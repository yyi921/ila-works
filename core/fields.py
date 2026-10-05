from django.db import models

from . import crypto


class EncryptedTextField(models.TextField):
    """Text stored encrypted in the database and decrypted on load. Empty strings are stored as-is.

    Encryption is non-deterministic, so the field can't be filtered or indexed on.
    """

    def from_db_value(self, value, expression, connection):
        if value in (None, ""):
            return value
        return crypto.decrypt(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value in (None, ""):
            return value
        return crypto.encrypt(value)
