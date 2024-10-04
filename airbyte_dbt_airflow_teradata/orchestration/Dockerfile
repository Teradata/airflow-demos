FROM apache/airflow:2.8.3-python3.11
COPY requirements.txt /
ENV dbt_base_dir=/opt/airflow/dbt_project
RUN pip install --no-cache-dir -r /requirements.txt