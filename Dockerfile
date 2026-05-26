FROM python:3.12-alpine

RUN apk add --no-cache \
    pango \
    fontconfig \
    ttf-dejavu \
    harfbuzz \
    jpeg-dev \
    zlib-dev \
    libffi-dev \
    gcc \
    musl-dev

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mail-pdf-bridge.py .

VOLUME ["/consume", "/eml"]

CMD ["python", "-u", "mail-pdf-bridge.py"]
