from cryptography.fernet import Fernet

from app.common.config import settings


cipher = Fernet(
    settings.MASTER_KEY.encode()
)


def encrypt_value(value: str) -> str:
    encrypted = cipher.encrypt(
        value.encode()
    )

    return encrypted.decode()


def decrypt_value(value: str) -> str:
    decrypted = cipher.decrypt(
        value.encode()
    )

    return decrypted.decode()