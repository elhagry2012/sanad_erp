FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SANAD_ENV=production \
    SANAD_DB_PATH=/data/sanad.db \
    SANAD_BACKUP_DIR=/data/backups \
    SANAD_IMAGES_DIR=/data/invoice_images
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# تطبيق الويب القابل للتثبيت (PWA): ملفات المشروع + الوضع الكامل (Service Worker على مستوى الموقع) على نسخة الحاوية من Streamlit
RUN python -m tools.pwa_setup && python -m tools.pwa_setup --full
RUN useradd -m app && mkdir -p /data && chown -R app /app /data
USER app
VOLUME /data
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8501/_stcore/health')"
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
