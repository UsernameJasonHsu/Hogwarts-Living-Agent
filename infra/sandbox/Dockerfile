FROM python:3.12-slim
COPY runner.py /opt/hogwarts/runner.py
RUN mkdir /workspace /output && chmod 0555 /workspace /opt/hogwarts /opt/hogwarts/runner.py
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/python", "-I", "-B", "/opt/hogwarts/runner.py"]
