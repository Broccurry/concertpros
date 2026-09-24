"""Encrypting real external-account credentials at rest -- currently just
each person's own email app-password (see email_accounts in schema.sql).
This is not app data like everything else in the database; it's a login
credential for someone's actual mailbox, so it doesn't sit in Postgres as
plaintext the way a note or a deal figure would.

EMAIL_ENCRYPTION_KEY must be a Fernet key (44-char urlsafe-base64 string).
Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import os

from cryptography.fernet import Fernet


def _fernet():
    return Fernet(os.environ["EMAIL_ENCRYPTION_KEY"].encode())


def encrypt(plaintext):
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token):
    return _fernet().decrypt(token.encode()).decode()
