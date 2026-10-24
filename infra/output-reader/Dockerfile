FROM python:3.12-slim
COPY reader.py /opt/hogwarts-output-reader.py
RUN mkdir /output && chmod 0555 /opt/hogwarts-output-reader.py
USER 10001:10001
WORKDIR /output
ENTRYPOINT ["/usr/local/bin/python", "-I", "-B", "/opt/hogwarts-output-reader.py"]
