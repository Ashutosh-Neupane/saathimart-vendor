FROM python:3.14-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/frappe/.local/bin:/home/frappe/.cargo/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl git gnupg mariadb-client redis-tools netcat-openbsd \
      wkhtmltopdf xvfb libfontconfig1 cron pkg-config \
      default-libmysqlclient-dev \
      build-essential python3-dev \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g yarn \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

RUN pip install frappe-bench

WORKDIR /home/frappe
RUN useradd -ms /bin/bash frappe && chown frappe:frappe /home/frappe
USER frappe

# Pre-create bench dir so Docker volume mount inherits frappe ownership
RUN mkdir -p /home/frappe/bench /home/frappe/logs

COPY --chown=frappe:frappe docker/init.sh /usr/local/bin/init.sh
RUN chmod +x /usr/local/bin/init.sh

# Copy the whole app repo (not just the inner module dir) so bench sees
# the standard apps/<app>/<app>/ layout it needs to recognize the app
COPY --chown=frappe:frappe . /home/frappe/saathimart_vendor/

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
  CMD curl -sf -H "Host: vendor1.localhost" http://localhost:8000/api/method/ping || exit 1

CMD ["/usr/local/bin/init.sh"]
