"""
Image Processing Pipeline for CNIC and Document Onboarding.

Features:
- EXIF orientation correction
- OpenCV Document Detection (edge detection, perspective warp, crop)
- Advanced Document Enhancement (Denoising, CLAHE contrast, sharpening)
- Specular Glare Detection and Inpainting
- OCR Text Orientation Detection & Auto-rotation (via pytesseract with graceful fallback)
- Watermark Overlay Support
- WebP Adaptive Quality Compression
"""
import os
import io
import shutil
import hashlib
import numpy as np
from pathlib import Path
from PIL import Image, ImageOps, ImageEnhance

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import pytesseract
except ImportError:
    pytesseract = None


def configure_tesseract() -> str | None:
    """
    Point pytesseract at the tesseract binary.
    Colab/Linux usually have `tesseract` on PATH; Windows installs often do not.
    Override with env TESSERACT_CMD if needed.
    """
    if pytesseract is None:
        return None

    cmd = os.environ.get("TESSERACT_CMD")
    if cmd and os.path.isfile(cmd):
        pytesseract.pytesseract.tesseract_cmd = cmd
        return cmd

    found = shutil.which("tesseract")
    if found:
        pytesseract.pytesseract.tesseract_cmd = found
        return found

    if os.name == "nt":
        for path in (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        ):
            if os.path.isfile(path):
                pytesseract.pytesseract.tesseract_cmd = path
                return path

    return None


TESSERACT_PATH = configure_tesseract()


def _fix_orientation(image: Image.Image) -> Image.Image:
    """Auto-rotates and flips the image based on its EXIF orientation tag."""
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:
        pass
    return image


def _osd_candidates(image: Image.Image) -> list[Image.Image]:
    """Build variants — card photos often fail OSD on raw RGB alone."""
    rgb = image.convert("RGB") if image.mode != "RGB" else image
    gray = image.convert("L")
    w, h = gray.size
    if max(w, h) < 1600:
        gray = gray.resize((w * 2, h * 2), Image.Resampling.LANCZOS)
    return [
        gray,
        ImageEnhance.Contrast(gray).enhance(2.0),
        rgb,
    ]


def _detect_osd_angle(image: Image.Image) -> int | None:
    if pytesseract is None:
        return None

    last_err: Exception | None = None
    for candidate in _osd_candidates(image):
        try:
            osd = pytesseract.image_to_osd(
                candidate,
                output_type=pytesseract.Output.DICT,
            )
            return int(osd.get("rotate", 0))
        except pytesseract.TesseractError as e:
            last_err = e
        except Exception as e:
            last_err = e

    if last_err is not None:
        pass
    return None


def auto_rotate_by_text(image: Image.Image) -> Image.Image:
    """Looks at the text in the image and automatically rotates it upright."""
    if configure_tesseract() is None:
        return image

    try:
        angle = _detect_osd_angle(image)
        if angle is None:
            return image
        if angle != 0:
            image = image.rotate(-angle, expand=True)
    except Exception as e:
        print(f"Text orientation check skipped: {e}")

    return image


import uuid

def add_watermark(base_image: Image.Image, watermark_path: str, opacity: float = 0.50) -> Image.Image:
    """
    Overlays a transparent watermark onto the CENTER of the image.
    Automatically scales the watermark and adjusts its opacity.
    """
    try:
        if not watermark_path or not os.path.exists(watermark_path):
            return base_image

        watermark = Image.open(watermark_path).convert("RGBA")
        base_width, base_height = base_image.size
        wm_width, wm_height = watermark.size

        # Scaled up watermark: 55% of document width
        target_width = int(base_width * 0.55)
        if target_width <= 0:
            return base_image

        target_height = int((target_width / wm_width) * wm_height)
        watermark = watermark.resize((target_width, target_height), Image.Resampling.LANCZOS)

        alpha = watermark.getchannel('A')
        alpha = ImageEnhance.Brightness(alpha).enhance(opacity)
        watermark.putalpha(alpha)

        x = (base_width - target_width) // 2
        y = (base_height - target_height) // 2

        base_image = base_image.convert("RGBA") if base_image.mode != "RGBA" else base_image
        base_image.paste(watermark, (x, y), mask=watermark)
        base_image = base_image.convert("RGB")
    except Exception as e:
        print(f"Failed to apply watermark: {e}")

    return base_image


# OpenCV Processing Functions
def order_points(pts: np.ndarray) -> np.ndarray:
    """Sort 4 points into [Top-Left, Top-Right, Bottom-Right, Bottom-Left]."""
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1)
    rect[0] = pts[np.argmin(s)]     # TL
    rect[2] = pts[np.argmax(s)]     # BR
    rect[1] = pts[np.argmin(diff)]  # TR
    rect[3] = pts[np.argmax(diff)]  # BL
    return rect


def compute_output_size(rect: np.ndarray) -> tuple[int, int]:
    """Compute output width/height from the actual detected corners."""
    tl, tr, br, bl = rect
    width = max(int(np.linalg.norm(br - bl)), int(np.linalg.norm(tr - tl)))
    height = max(int(np.linalg.norm(tr - br)), int(np.linalg.norm(tl - bl)))
    return max(width, 100), max(height, 100)


def remove_glare(image: np.ndarray) -> np.ndarray:
    """Detects and inpaints specular reflection (glare) spots."""
    if cv2 is None:
        return image
    try:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        h, s, v = cv2.split(hsv)
        _, v_mask = cv2.threshold(v, 240, 255, cv2.THRESH_BINARY)
        _, s_mask = cv2.threshold(s, 30, 255, cv2.THRESH_BINARY_INV)
        combined = cv2.bitwise_and(v_mask, s_mask)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        combined = cv2.morphologyEx(combined, cv2.MORPH_OPEN, kernel)
        combined = cv2.dilate(combined, kernel, iterations=1)
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(combined)
        glare_mask = np.zeros_like(combined)
        max_glare_area = 500
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            w = stats[i, cv2.CC_STAT_WIDTH]
            hh = stats[i, cv2.CC_STAT_HEIGHT]
            aspect = max(w, hh) / (min(w, hh) + 1e-5)
            if area < max_glare_area and aspect < 3.0:
                glare_mask[labels == i] = 255

        if glare_mask.max() == 0:
            return image
        inpainted = cv2.inpaint(image, glare_mask, inpaintRadius=7, flags=cv2.INPAINT_TELEA)
        return inpainted
    except Exception:
        return image


def enhance_document(image: np.ndarray) -> np.ndarray:
    """Advanced high-speed enhancement: Denoising + CLAHE + Sharpening."""
    if cv2 is None:
        return image
    try:
        denoised = cv2.medianBlur(image, 3)
        lab = cv2.cvtColor(denoised, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        l = clahe.apply(l)
        enhanced = cv2.merge((l, a, b))
        enhanced = cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)
        gaussian_blur = cv2.GaussianBlur(enhanced, (0, 0), 3)
        final_output = cv2.addWeighted(enhanced, 1.5, gaussian_blur, -0.5, 0)
        return final_output
    except Exception:
        return image


def scan_document(image: np.ndarray) -> np.ndarray:
    """Finds the document boundaries and warps it to a flat rectangle."""
    if cv2 is None:
        return image

    try:
        img_height, img_width = image.shape[:2]
        original = image.copy()

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.bilateralFilter(gray, 9, 75, 75)
        thresh = cv2.adaptiveThreshold(
            blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 11, 2
        )
        edged = cv2.Canny(thresh, 30, 150)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        edged = cv2.dilate(edged, kernel, iterations=1)

        contours, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]

        document_contour = None
        min_area = (img_width * img_height) * 0.1

        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area:
                continue
            perimeter = cv2.arcLength(c, True)
            approx = cv2.approxPolyDP(c, 0.02 * perimeter, True)
            if len(approx) == 4:
                document_contour = approx
                break

        if document_contour is None and len(contours) > 0:
            if cv2.contourArea(contours[0]) > min_area:
                rect = cv2.minAreaRect(contours[0])
                box = cv2.boxPoints(rect)
                document_contour = np.int32(box).reshape(4, 1, 2)

        if document_contour is None:
            return enhance_document(original)

        pts = document_contour.reshape(4, 2).astype(np.float32)
        rect = order_points(pts)
        width, height = compute_output_size(rect)

        dst_pts = np.array([[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]], dtype="float32")
        matrix = cv2.getPerspectiveTransform(rect, dst_pts)
        scanned_image = cv2.warpPerspective(original, matrix, (width, height))

        return enhance_document(scanned_image)
    except Exception as e:
        print(f"scan_document fallback: {e}")
        return image


def process_transaction_pipeline(
    input_bytes: bytes,
    original_filename: str,
    watermark_path: str = None,
    process_cv: bool = True
) -> dict:
    """
    Unified master pipeline:
    1. EXIF orientation fix
    2. OpenCV document scanning, corner detection, perspective crop & enhancement (if process_cv=True)
    3. Tesseract OCR auto-rotation (if process_cv=True)
    4. Optional watermark
    5. Adaptive WebP compression loop
    """
    original_hash = hashlib.sha256(input_bytes).hexdigest()
    original_size = len(input_bytes)

    # 1. Load Pillow image and fix orientation
    image = Image.open(io.BytesIO(input_bytes))
    image = _fix_orientation(image)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")

    # 2. Convert to OpenCV BGR array if OpenCV is available and document processing is requested
    if process_cv and cv2 is not None:
        cv_image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        scanned_cv_image = scan_document(cv_image)
        scanned_pil_image = Image.fromarray(cv2.cvtColor(scanned_cv_image, cv2.COLOR_BGR2RGB))
    else:
        scanned_pil_image = image

    # 3. Auto-rotate via OCR if document processing is requested
    if process_cv:
        final_image = auto_rotate_by_text(scanned_pil_image)
    else:
        final_image = scanned_pil_image

    # 4. Optional watermark
    if watermark_path and os.path.exists(watermark_path):
        final_image = add_watermark(final_image, watermark_path)

    # 5. Save with WebP compression
    quality = 80
    buf = io.BytesIO()
    final_image.save(buf, format="WEBP", lossless=False, method=6, quality=quality)

    while len(buf.getvalue()) > original_size and quality >= 50:
        quality -= 15
        buf = io.BytesIO()
        final_image.save(buf, format="WEBP", lossless=False, method=6, quality=quality)

    compressed = buf.getvalue()
    compressed_size = len(compressed)
    savings = ((original_size - compressed_size) / original_size) * 100 if original_size > 0 else 0.0

    random_filename = f"{uuid.uuid4().hex}.webp"
    return {
        "data": compressed,
        "filename": random_filename,
        "mime_type": "image/webp",
        "original_hash": original_hash,
        "original_size": original_size,
        "compressed_size": compressed_size,
        "savings_percent": round(savings, 1),
    }


def process_uploaded_image(uploaded_file, watermark_path: str = None, process_cv: bool = True):
    """
    Wrapper for Django UploadedFile. Processes uploaded file bytes and returns
    a ContentFile ready for storage in Django ImageField / FileField.
    """
    from django.core.files.base import ContentFile

    if hasattr(uploaded_file, "read"):
        input_bytes = uploaded_file.read()
        filename = getattr(uploaded_file, "name", "upload.jpg")
        uploaded_file.seek(0)
    elif isinstance(uploaded_file, bytes):
        input_bytes = uploaded_file
        filename = "upload.jpg"
    else:
        return uploaded_file

    processed = process_transaction_pipeline(
        input_bytes,
        filename,
        watermark_path=watermark_path,
        process_cv=process_cv
    )
    content_file = ContentFile(processed["data"], name=processed["filename"])
    return content_file
