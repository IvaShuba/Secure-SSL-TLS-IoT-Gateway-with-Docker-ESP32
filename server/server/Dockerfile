FROM python:3.11-slim

RUN apt-get update && apt-get install -y protobuf-compiler

WORKDIR /app



# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Копируем requirements
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt


# Копируем приложение
COPY . .


# Запуск uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8443", "--ssl-keyfile", "/certs/server_app.key", "--ssl-certfile", "/certs/server_app.crt"]

