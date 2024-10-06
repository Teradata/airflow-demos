from pendulum import datetime
from airflow.decorators import dag
from airflow.providers.airbyte.operators.airbyte import AirbyteTriggerSyncOperator
from airflow.operators.trigger_dagrun import TriggerDagRunOperator

from cosmos import DbtDag
from cosmos.config import RenderConfig

from dbt_config import project_config, profile_config



@dag(
    dag_id="elt_dag",
    start_date=datetime(2023, 10, 1),
    schedule="@daily",
    tags=["airbyte", "dbt", "teradata", "ecommerce"],
    catchup=False,
)
def extract_and_transform():
    """
    Runs the connection "Faker to Teradata" on Airbyte and then triggers the dbt DAG.
    """
    # Airbyte sync task
    extract_data = AirbyteTriggerSyncOperator(
        task_id="trigger_airbyte_faker_to_teradata",
        airbyte_conn_id="airbyte_connection",
        connection_id="#######", # Update with your Airbyte connection ID
        asynchronous=False,
        timeout=3600,
        wait_seconds=3
    )

    # Trigger for dbt DAG
    trigger_dbt_dag = TriggerDagRunOperator(
        task_id="trigger_dbt_dag",
        trigger_dag_id="dbt_job_trigger",
        wait_for_completion=True,
        poke_interval=30,
    )


    # Set the order of tasks
    extract_data >> trigger_dbt_dag

# Instantiate the ELT DAG
extract_and_transform_dag = extract_and_transform()

# Define dbt transform
dbt_cosmos_dag = DbtDag(
    dag_id="dbt_dag",
    start_date=datetime(2023, 10, 1),
    tags=["dbt", "ecommerce"],
    catchup=False,
    project_config=project_config,
    profile_config=profile_config,
    render_config=RenderConfig(select=["path:models/ecommerce"]),
)
    
dbt_cosmos_dag