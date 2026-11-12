# python:3.12-alpine, index digest looked up 2026-11-04
FROM python@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8 AS build
WORKDIR /src
COPY site/ site/
COPY scripts/ scripts/
COPY executors/ executors/
COPY README.md CONTRIBUTING.md LICENSE venom.gif ./
COPY tests/assertions_operators.yml tests/ranged.yml tests/couchbase.yml tests/imap.yml tests/mongo.yml tests/
COPY deploy/site/nginx.conf deploy/site/nginx.conf
RUN python3 scripts/build_site.py --out /out

# nginxinc/nginx-unprivileged:1.28-alpine, index digest looked up 2026-11-04
FROM nginxinc/nginx-unprivileged@sha256:7377697a821c131a924a7105fafbe7414db4e9fcc77a6f08f776f33f141ec3f8
USER root
RUN rm -rf /usr/share/nginx/html/*
COPY --from=build /out/ /usr/share/nginx/html/
COPY --from=build /src/deploy/site/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /src/LICENSE /licenses/LICENSE
USER 101:101
EXPOSE 8080
