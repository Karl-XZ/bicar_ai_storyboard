import base64
import hashlib
import hmac
import json

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


def verify_legacy_signature(*, timestamp: str, nonce: str, encrypt_key: str, signature: str) -> bool:
    payload = f"{timestamp}{nonce}{encrypt_key}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return hmac.compare_digest(digest, signature)


def parse_plain_event(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


def parse_event_body(body: bytes, encrypt_key: str = "") -> dict:
    payload = parse_plain_event(body)
    encrypted = payload.get("encrypt")
    if encrypted and encrypt_key:
        return json.loads(decrypt_event(encrypted, encrypt_key))
    return payload


def decrypt_event(encrypted: str, encrypt_key: str) -> str:
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    plaintext = unpad(cipher.decrypt(base64.b64decode(encrypted)), AES.block_size)
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        # Some callback payloads include a 16-byte random prefix before JSON.
        return plaintext[16:].decode("utf-8")


def safe_base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
