from pendulum import datetime
from airflow.decorators import dag
from airflow.providers.airbyte.operators.airbyte import AirbyteTriggerSyncOperator
from airflow.operators.bash import BashOperator

# Define the dbt directory variable
dbt_dir = "/opt/airflow/dbt_project"

@dag(
    dag_id="elt_dag_combined",
    start_date=datetime(2023, 10, 1),
    schedule="@daily",
    tags=["airbyte", "dbt", "teradata", "ecommerce"],
    catchup=False,
)
def extract_transform_load():
    """
    Runs the connection "Faker to Teradata" on Airbyte and then triggers the dbt transformation.
    """

    # Airbyte sync task
    extract_data = AirbyteTriggerSyncOperator(
        task_id="trigger_airbyte_faker_to_teradata",
        airbyte_conn_id="airbyte_connection",
        connection_id="0c001934-f781-45c0-9a27-6af19c26cb02",  # Update with your Airbyte connection ID
        asynchronous=False,
        timeout=3600,
        wait_seconds=3
    )

    # dbt transformation task
    transform_data = BashOperator(
        task_id="transform_data",
        bash_command=f"dbt run --profiles-dir {dbt_dir} --project-dir {dbt_dir} -s path:models/ecommerce"
    )

    # Task dependencies
    extract_data >> transform_data

# Instantiate the DAG
extract_transform_load()
