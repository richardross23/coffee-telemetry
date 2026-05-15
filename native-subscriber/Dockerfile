FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir --root-user-action=ignore aioesphomeapi==25.* \
    && rm -rf /root/.cache/pip

COPY native_subscriber.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "native_subscriber.py"]
