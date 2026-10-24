FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/images

ENV DB_PATH=/app/shopify_scraper.db
ENV IMAGES_PATH=/app/images

CMD ["python", "start.py"]
