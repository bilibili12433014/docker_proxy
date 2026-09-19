FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_PROXY_CONFIG=/workspace/config.yaml

WORKDIR /launcher

RUN pip install --no-cache-dir --upgrade asyncssh docker Flask PyYAML

COPY app ./app
COPY start.py ./start.py

EXPOSE 2221 2222

CMD ["python", "/launcher/start.py"]
