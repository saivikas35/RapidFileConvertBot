FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ghostscript \
    poppler-utils \
    libreoffice \
    fonts-dejavu-core \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

ENV PATH="/usr/bin:${PATH}"

CMD ["python", "bot.py"]
