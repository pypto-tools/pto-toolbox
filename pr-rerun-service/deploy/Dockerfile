FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.lock /app/
COPY src /app/src
COPY docs /app/docs
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home rerun \
    && mkdir /data && chown rerun:rerun /data
USER rerun
ENV PR_RERUN_DATABASE=/data/rerun.sqlite
ENTRYPOINT ["pr-rerun-server", "--local-only"]
