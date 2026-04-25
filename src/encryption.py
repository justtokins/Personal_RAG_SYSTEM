"""
encryption.py — AES-128 Fernet encryption for content at rest.
"""
import os
from typing import Union

from cryptography.fernet import Fernet, InvalidToken
from src.logger import get_logger

logger = get_logger()


class Encryption:
    def __init__(self, key: Union[str, bytes, None] = None):
        if key is None:
            key = os.getenv("ENCRYPTION_KEY")

        if key is None:
            self.key = Fernet.generate_key()
            logger.warning(
                "ENCRYPTION | No ENCRYPTION_KEY env var — ephemeral key generated. "
                f"Add to .env: ENCRYPTION_KEY={self.key.decode()}"
            )
        else:
            self.key = key.encode() if isinstance(key, str) else key
            logger.info("ENCRYPTION | Key loaded from environment")

        self.cipher = Fernet(self.key)

    def encrypt(self, plaintext: str) -> str:
        return self.cipher.encrypt(str(plaintext).encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self.cipher.decrypt(str(ciphertext).encode()).decode()
        except InvalidToken:
            logger.error("ENCRYPTION | Decryption failed — wrong key or tampered data")
            raise

    def encrypt_bytes(self, data: bytes) -> bytes:
        return self.cipher.encrypt(data)

    def decrypt_bytes(self, data: bytes) -> bytes:
        return self.cipher.decrypt(data)


_enc: Union[Encryption, None] = None


def get_encryption() -> Encryption:
    global _enc
    if _enc is None:
        _enc = Encryption()
    return _enc