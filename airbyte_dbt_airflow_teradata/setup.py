from setuptools import find_packages, setup

setup(
    name="airbyte-dbt-airflow-teradata",
    packages=find_packages(),
    install_requires=[
        "dbt-teradata",
        "astronomer-cosmos",
        "apache-airflow-providers-airbyte",
        "apache-airflow",
        "apache-airflow-providers-teradata"
    ],
    extras_require={"dev": ["pytest"]},
)
