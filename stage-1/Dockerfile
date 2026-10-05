# Standard library only: nothing is fetched at run time.
FROM python:3.12-alpine
WORKDIR /app
COPY app.py wallet.py passwords.py ./
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s \
  CMD wget -qO- "http://127.0.0.1:${PORT}/health" >/dev/null || exit 1
CMD ["python", "-u", "app.py"]
