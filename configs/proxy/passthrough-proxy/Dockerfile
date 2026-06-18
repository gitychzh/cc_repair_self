# R31.6: passthrough-role proxy — physically isolated from cc/codex gateways.
# Independent image: a change here cannot affect 40001(cc)/40002(codex)/40005(cc).
FROM ghcr.io/berriai/litellm:v1.83.14-stable.patch.1
WORKDIR /app
RUN mkdir -p /app/logs
COPY gateway/ ./gateway/
COPY gateway_main.py .
ENTRYPOINT ["python3"]
CMD ["gateway_main.py"]
