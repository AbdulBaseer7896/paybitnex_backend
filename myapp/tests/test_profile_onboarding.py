"""Tests for CustomerProfile onboarding validation."""
import io
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from myapp.Models.Auth_models import User, UserRole
from myapp.Models.Profile_models import CustomerProfile


def _make_test_image(name="doc.png"):
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), color=(255, 255, 255)).save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    SECURE_SSL_REDIRECT=False,
)
class CustomerProfileOnboardingTests(TestCase):
    """Ensure onboarding profile creation enforces required KYC fields."""

    def setUp(self):
        self.customer = User.objects.create(
            email="onboard_test@example.com",
            full_name="Onboard Test",
            role=UserRole.CUSTOMER,
            is_active=True,
            email_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.customer)

    def test_post_onboarding_without_cnic_number_returns_400(self):
        """POST /api/v1/accounts/profile/ without cnic_number must return 400."""
        payload = {
            "full_name": "Onboard Test",
            "phone": "+923001234567",
            "address": "123 Main St",
            "city": "Lahore",
            "cnic_front": _make_test_image("front.png"),
            "cnic_back": _make_test_image("back.png"),
        }
        response = self.client.post(
            "/api/v1/accounts/profile/",
            data=payload,
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cnic_number", response.data)
        self.assertFalse(CustomerProfile.objects.filter(user=self.customer).exists())
