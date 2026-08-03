FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# --workers 1 is required: bot sessions and the asyncio event loop used to
# bridge python-telegram-bot into sync Flask live in process memory, so
# multiple worker processes would each keep their own (inconsistent) copy.
# --threads gives concurrency for regular HTTP requests without that problem.
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 60"]
