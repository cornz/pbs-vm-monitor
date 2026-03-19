FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY check-vm.py ./

RUN chmod +x /app/check-vm.py

USER appuser

EXPOSE 8081

ENTRYPOINT ["./check-vm.py"]
CMD ["serve"]
