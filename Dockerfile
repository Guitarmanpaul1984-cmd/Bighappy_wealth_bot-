FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir solders
COPY . /app
CMD ["python", "-u", "/app/live_overlay.py"]
