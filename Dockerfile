FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/var/lib/college-bot

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --user-group --create-home bot \
    && install -d -o bot -g bot "$DATA_DIR"

COPY college_bot/ college_bot/
USER bot
CMD ["python", "-m", "college_bot"]
