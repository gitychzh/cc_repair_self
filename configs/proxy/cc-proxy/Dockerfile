# R31.5: cc-role proxy — physically isolated from codex/passthrough gateways.
# Same litellm base image as the shared gateway, but copies ONLY the cc-relevant
# code (no codex.py, no OpenAI passthrough handler). A change here cannot affect
# the 40002/40003 containers, which build from configs/proxy/ (the shared tree).
FROM ghcr.io/berriai/litellm:v1.83.14-stable.patch.1
WORKDIR /app
RUN mkdir -p /app/logs
COPY gateway/ ./gateway/
COPY gateway_main.py .
ENTRYPOINT ["python3"]
CMD ["gateway_main.py"]
