# R37: Hermes专用 NV proxy — 物理隔离 from cc/codex/passthrough gateways.
# Independent image: a change here cannot affect 40001/40002/40003/40005.
FROM ghcr.io/berriai/litellm:v1.83.14-stable.patch.1
WORKDIR /app
RUN mkdir -p /app/logs
COPY gateway/ ./gateway/
COPY gateway_main.py .
ENTRYPOINT ["python3"]
CMD ["gateway_main.py"]
