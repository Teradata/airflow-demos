from setuptools import find_packages, setup

setup(
    name="airbyte-dbt-airflow-teradata",
    packages=find_packages(),
    install_requires=[
        "dbt-teradata",
        "apache-airflow-providers-teradata",
        "apache-airflow-providers-airbyte",
        "apache-airflow",
    ],
    extras_require={"dev": ["pytest"]},
)


