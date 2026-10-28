# syntax=docker/dockerfile:1

FROM python:3.12-alpine

LABEL org.opencontainers.image.title="docker-notes" \
      org.opencontainers.image.description="Docker Compose 中文备注 / 端口 / CPU / 内存 / 网络看板" \
      org.opencontainers.image.licenses="MIT"

# docker-cli      -> docker ps / inspect / stats
# docker-cli-compose -> docker compose ls（读项目列表必须）
RUN apk add --no-cache docker-cli docker-cli-compose tzdata

WORKDIR /app
COPY docker_notes.py /app/docker_notes.py
COPY compose-labels.example.yml /app/compose-labels.example.yml

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=UTF-8 \
    LANG=C.UTF-8 \
    PORT=8080 \
    REFRESH_SECONDS=300 \
    TZ=Asia/Shanghai

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"

ENTRYPOINT ["python", "/app/docker_notes.py"]
CMD ["serve"]
