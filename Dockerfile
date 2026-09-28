FROM python:3.12-slim
WORKDIR /app
COPY bot.py /app/bot.py
CMD ["python", "-u", "/app/bot.py"]
