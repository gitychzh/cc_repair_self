# R38.10: Hermes专用 NV proxy — NVCF pexec direct path for deepseek (SOCKS5 via mihomo).
# Independent image: a change here cannot affect 40001/40002/40003/40005.
# R40: psycopg2-binary for structured metrics persistence to cc_postgres (hermes_logs DB).
FROM ghcr.io/berriai/litellm:v1.83.14-stable.patch.1
WORKDIR /app
# PySocks for SOCKS5 proxy support (NVCF pexec bypasses DEGRADING integrate API)
# psycopg2-binary for R40 DB logging (binary wheel — no pg_config / build-essential needed)
RUN python3 -m ensurepip --default-pip 2>/dev/null && \
    python3 -m pip install --quiet "PySocks>=1.7.1" "psycopg2-binary>=2.9"
RUN mkdir -p /app/logs
COPY gateway/ ./gateway/
COPY gateway_main.py .
ENTRYPOINT ["python3"]
CMD ["gateway_main.py"]
