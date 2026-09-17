FROM python:3.14-slim

LABEL org.opencontainers.image.source="https://github.com/ibvadim/local-label" \
      org.opencontainers.image.description="Local-first TSPL label designer and printer" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY static ./static

# PyUSB is deliberately installed in the image, not exposed as a user setting.
# libusb is required by its Linux backend for direct server-side USB printing.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir fastapi pillow python-multipart qrcode sqlalchemy "uvicorn[standard]" pyusb

ENV LOCALLABEL_DATABASE_URL=sqlite:////data/locallabel.db
VOLUME ["/data"]
EXPOSE 8811
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8811"]
