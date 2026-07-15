FROM apache/airflow:3.3.0-python3.11

ARG PYTHON_VERSION=3.11

COPY requirements.txt /

RUN pip install --no-cache-dir -r /requirements.txt