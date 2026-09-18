FROM python:3.11-slim

LABEL maintainer="BongerSingara Team"
LABEL description="GridWise Energy Optimizer — BUP CSE Fest 2026"

WORKDIR /app

# Install dependencies first for Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ ./app/

# Expose the service port
EXPOSE 8000

# Health check (judge expects /health ready within 60s)
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run with uvicorn, bind to 0.0.0.0
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

