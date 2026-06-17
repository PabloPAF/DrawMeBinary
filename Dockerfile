# DrawMeBinary – Hugging Face Spaces (Docker SDK)
#
# Constraints:
#   * HF Spaces requires the app to listen on port 7860
#   * HF runs containers as a non-root user (uid 1000)
#   * opencv must be the headless variant (no display available)
#   * tesseract-ocr must be installed as a system package
#   * DejaVu fonts needed (preferred_font = 'DejaVuSans' in config.py)
#
# Build locally to test before pushing:
#   docker build -t dmb .
#   docker run --rm -p 7860:7860 dmb
#   open http://localhost:7860

FROM python:3.11-slim

# ── system dependencies ────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        # opencv-headless runtime libs
        libgl1 \
        libglib2.0-0 \
        # fonts (config.py preferred_font = 'DejaVuSans')
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# ── non-root user (uid 1000 matches HF Spaces runtime) ────────────────────────
RUN useradd -m -u 1000 appuser

# ── python dependencies ────────────────────────────────────────────────────────
WORKDIR /app

# Install deps before copying source so Docker can cache this layer.
# opencv-python-headless replaces opencv-python (no GUI needed in a container).
# gunicorn replaces Flask's dev server for production.
# pytest is excluded (not needed at runtime).
RUN pip install --no-cache-dir \
        "opencv-python-headless>=4.8" \
        "numpy>=1.24" \
        "pillow>=10.0" \
        "pytesseract>=0.3.10" \
        "pyspellchecker>=0.8" \
        "pymupdf>=1.24" \
        "flask>=3.0" \
        "gunicorn>=21.2"

# ── application source ─────────────────────────────────────────────────────────
COPY drawmebinary/  ./drawmebinary/
COPY webapp/        ./webapp/
# Keras model path resolves to /app/mnist_binary_verifier.keras (see config.py)
COPY mnist_binary_verifier.keras .

# Writable dirs for logs and render output (config.py log_dir / output_dir)
RUN mkdir -p logs output && chown -R appuser:appuser /app

# ── runtime ────────────────────────────────────────────────────────────────────
USER appuser

EXPOSE 7860

# --chdir keeps __file__-relative imports in app.py working correctly.
# 1 worker: free-tier CPU is single-core; timeout 120s for slow images.
CMD ["gunicorn", \
     "--chdir", "/app/webapp", \
     "--bind", "0.0.0.0:7860", \
     "--workers", "1", \
     "--timeout", "120", \
     "app:app"]
