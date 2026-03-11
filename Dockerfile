FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

ENV PORT=10000
ENV PLAYWRIGHT_HEADLESS=true

EXPOSE 10000

CMD ["python", "worker.py"]
