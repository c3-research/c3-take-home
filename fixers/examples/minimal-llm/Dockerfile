# docker build --network host --platform linux/amd64 -t c3-fixer-minimal-llm fixers/examples/minimal-llm
FROM --platform=linux/amd64 python:3.12.12-slim-bookworm
RUN pip install --no-cache-dir pyyaml pytest
WORKDIR /fixer
COPY fix.py run.sh /fixer/
ENTRYPOINT ["/bin/sh", "/fixer/run.sh"]
