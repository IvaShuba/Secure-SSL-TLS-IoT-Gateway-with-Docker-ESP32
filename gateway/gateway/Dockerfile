FROM python:3.9-slim

WORKDIR /app

# 1. Устанавливаем компилятор и библиотеки для математики (нужны для numpy)
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# 2. Устанавливаем пакеты.
# ВАЖНО: --extra-index-url https://www.piwheels.org/simple заставляет pip
# искать готовые колеса (wheels) специально для Raspberry Pi.
RUN pip install --no-cache-dir -r requirements.txt --extra-index-url https://www.piwheels.org/simple

COPY src/. .

CMD ["python", "-u", "main.py"]

