FROM python:3.12-alpine
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 LANG=C.UTF-8 \
    SYNOARR_CONFIG=/config/config.ini TZ=Europe/Paris
RUN apk add --no-cache tzdata
WORKDIR /app
COPY synoarr.py config.example.ini ./
COPY synoarr ./synoarr
VOLUME ["/config"]
EXPOSE 8778
ENTRYPOINT ["python3", "/app/synoarr.py"]
CMD ["run"]
