FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir --root-user-action=ignore paho-mqtt==2.* \
    && rm -rf /root/.cache/pip

COPY logger.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "logger.py"]
