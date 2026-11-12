"""微信支付协议的离线安全检查；不访问商户接口或业务库。"""

import base64
import json
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.test import SimpleTestCase, override_settings

from apps.billing.wechat_pay import PaymentError, _one_month_later, decrypt_notification, verify_wechat_signature


class WechatPayProtocolTest(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_path = Path(self.tmp.name) / "wechat_public.pem"
        public_path.write_bytes(self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ))
        context = override_settings(
            WECHAT_PAY_APP_ID="app", WECHAT_PAY_MCH_ID="mch", WECHAT_PAY_MCH_SERIAL_NO="merchant",
            WECHAT_PAY_PRIVATE_KEY_PATH=str(public_path), WECHAT_PAY_PUBLIC_KEY_ID="platform",
            WECHAT_PAY_PUBLIC_KEY_PATH=str(public_path), WECHAT_PAY_API_V3_KEY="k" * 32,
            WECHAT_PAY_NOTIFY_URL="https://example.com/notify",
        )
        context.enable()
        self.addCleanup(context.disable)

    def _headers(self, body):
        timestamp = str(int(time.time()))
        nonce = "nonce123"
        signature = self.private.sign(
            f"{timestamp}\n{nonce}\n".encode() + body + b"\n", padding.PKCS1v15(), hashes.SHA256()
        )
        return {
            "Wechatpay-Timestamp": timestamp, "Wechatpay-Nonce": nonce,
            "Wechatpay-Serial": "platform", "Wechatpay-Signature": base64.b64encode(signature).decode(),
        }

    def test_signature_rejects_modified_body(self):
        body = b'{"id":"ok"}'
        verify_wechat_signature(self._headers(body), body)
        with self.assertRaises(PaymentError):
            verify_wechat_signature(self._headers(body), b'{"id":"changed"}')

    def test_notification_decrypt_and_reject_tampering(self):
        resource = {"algorithm": "AEAD_AES_256_GCM", "nonce": "123456789012", "associated_data": "wx"}
        ciphertext = AESGCM(b"k" * 32).encrypt(b"123456789012", b'{"trade_state":"SUCCESS"}', b"wx")
        resource["ciphertext"] = base64.b64encode(ciphertext).decode()
        body = json.dumps({"event_type": "TRANSACTION.SUCCESS", "resource": resource}).encode()
        self.assertEqual(decrypt_notification(body)["trade_state"], "SUCCESS")
        resource["associated_data"] = "tampered"
        with self.assertRaises(PaymentError):
            decrypt_notification(json.dumps({"event_type": "TRANSACTION.SUCCESS", "resource": resource}).encode())

    def test_month_end_renewal(self):
        from datetime import datetime, timezone
        self.assertEqual(_one_month_later(datetime(2026, 1, 31, tzinfo=timezone.utc)).day, 28)
