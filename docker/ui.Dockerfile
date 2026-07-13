# Shared image for both Streamlit UIs; compose picks the app via `command`.
# The UIs talk to the API over HTTP only, so they need none of the orchestrator
# package or its heavy dependencies.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN pip install "streamlit>=1.38" "httpx>=0.27"

COPY ui ./ui

EXPOSE 8501 8502
CMD ["streamlit", "run", "ui/review_app.py", "--server.port", "8501", "--server.address", "0.0.0.0", "--server.headless", "true"]
