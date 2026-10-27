FROM python:3.12-slim
RUN groupadd -g 10001 hogwarts && useradd -u 10001 -g 10001 -M hogwarts
COPY infra/sandbox/runner.py /opt/hogwarts/runner.py
COPY infra/review-qa/supervisor.py /opt/hogwarts/qa-supervisor.py
COPY infra/review-qa/tests.py /opt/hogwarts/review-tests.py
ARG REVIEW_EXPECTED="deterministic prototype"
ARG REVIEW_ZERO_TESTS=0
ENV REVIEW_EXPECTED=$REVIEW_EXPECTED REVIEW_ZERO_TESTS=$REVIEW_ZERO_TESTS
RUN chmod 0555 /opt/hogwarts/*.py
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/python"]
