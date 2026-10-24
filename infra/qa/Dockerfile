FROM python:3.12-slim
RUN groupadd -g 10001 hogwarts && useradd -u 10001 -g 10001 -M hogwarts
COPY infra/sandbox/runner.py /opt/hogwarts/runner.py
COPY infra/qa/supervisor.py /opt/hogwarts/qa-supervisor.py
RUN chmod 0555 /opt/hogwarts/*.py
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/python"]
