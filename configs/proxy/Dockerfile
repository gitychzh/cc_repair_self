FROM python:3.11-alpine
WORKDIR /app
RUN mkdir -p /app/logs
COPY proxy.py .
ENTRYPOINT ["python3"]
CMD ["proxy.py"]