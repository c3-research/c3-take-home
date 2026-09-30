# docker build --network host --platform linux/amd64 -t c3-fixer-noop fixers/examples/noop
FROM --platform=linux/amd64 python:3.12.12-slim-bookworm
RUN pip install --no-cache-dir pyyaml pytest
WORKDIR /fixer
COPY fix.py run.sh /fixer/
ENTRYPOINT ["/bin/sh", "/fixer/run.sh"]
