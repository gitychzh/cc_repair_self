# R31.7: CC model→endpoint dispatcher (pure stdlib Python, no deps).
# Reuse the local litellm image (already pulled, has python3) to avoid docker.io pulls.
FROM ghcr.io/berriai/litellm:v1.83.14-stable.patch.1
WORKDIR /app
RUN mkdir -p /app/logs
COPY gateway/ ./gateway/
COPY gateway_main.py .
ENTRYPOINT ["python3"]
CMD ["gateway_main.py"]
