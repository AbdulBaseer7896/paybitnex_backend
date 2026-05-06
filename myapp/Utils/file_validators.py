"""
Reusable file upload validators.
Enforces MIME type and file size on all user-uploaded images and documents.
"""
from django.core.exceptions import ValidationError

# 10 MB hard limit for all user uploads
MAX_IMAGE_SIZE = 10 * 1024 * 1024   # 10 MB
MAX_DOC_SIZE   = 15 * 1024 * 1024   # 15 MB (PDFs can be larger)

ALLOWED_IMAGE_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp",
}
ALLOWED_DOC_TYPES = ALLOWED_IMAGE_TYPES | {"application/pdf"}


def validate_image_file(file):
    """Validate that an uploaded file is an allowed image type and within size."""
    content_type = getattr(file, "content_type", None) or ""
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ValidationError(
            f"Unsupported file type '{content_type}'. "
            f"Allowed: JPEG, PNG, WEBP."
        )
    if file.size > MAX_IMAGE_SIZE:
        mb = MAX_IMAGE_SIZE // (1024 * 1024)
        raise ValidationError(f"File too large. Maximum allowed size is {mb} MB.")


def validate_doc_file(file):
    """Validate image or PDF upload."""
    content_type = getattr(file, "content_type", None) or ""
    if content_type not in ALLOWED_DOC_TYPES:
        raise ValidationError(
            f"Unsupported file type '{content_type}'. "
            f"Allowed: JPEG, PNG, WEBP, PDF."
        )
    if file.size > MAX_DOC_SIZE:
        mb = MAX_DOC_SIZE // (1024 * 1024)
        raise ValidationError(f"File too large. Maximum allowed size is {mb} MB.")
