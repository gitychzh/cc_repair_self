# ms-gateway: Lightweight ModelScope API gateway replacing LiteLLM 41001.
# Replaces the 1.5GB LiteLLM container with a ~50MB Python Alpine image.
# Function: receive OpenAI chat/completions with model_name (e.g. glm5.1v3k5),
# resolve variant ID + MS API key, forward to ModelScope directly via HTTPS.
# No routing, no retries, no cooldown — just a mapping table + HTTPS forward.
FROM python:3.11-alpine
WORKDIR /app
RUN mkdir -p /app/logs
COPY gateway/ ./gateway/
COPY gateway_main.py .
ENTRYPOINT ["python3"]
CMD ["gateway_main.py"]
