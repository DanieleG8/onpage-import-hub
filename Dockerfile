# Build deterministico per Railway: il builder automatico (Railpack/mise)
# falliva con "secret GH_TOKEN not found" nell'installazione di Python.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
