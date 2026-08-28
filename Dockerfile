FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py index.html ./
COPY journal ./journal
COPY static ./static

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "main.py"]
