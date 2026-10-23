FROM python:3.12-slim

# tesseract with the Russian pack is for scanned PDFs uploaded through the cabinet
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home korsik
WORKDIR /srv/korsik

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY tools tools
COPY scenario scenario
COPY kb-demo kb-demo
RUN mkdir data && chown korsik:korsik data scenario

ENV PYTHONUNBUFFERED=1
USER korsik
EXPOSE 8080
CMD ["python", "-m", "app.main"]
