FROM python:3.12.10-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PAPERFLOW_HOST=0.0.0.0 PORT=8888
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng fonts-noto-cjk fontconfig && rm -rf /var/lib/apt/lists/*
COPY requirements.lock.txt ./requirements.lock.txt
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY . .
RUN mkdir -p data && useradd --uid 10001 --create-home paperflow && chown -R paperflow:paperflow /app
ENTRYPOINT ["python", "scripts/container_entrypoint.py"]
EXPOSE 8888
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/api/health', timeout=4)"
CMD ["python", "run.py", "--host", "0.0.0.0"]
