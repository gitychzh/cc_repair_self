FROM ghcr.io/berriai/litellm:v1.83.14-stable.patch.1
WORKDIR /app
RUN mkdir -p /app/logs
COPY proxy.py .
ENTRYPOINT ["python3"]
CMD ["proxy.py"]