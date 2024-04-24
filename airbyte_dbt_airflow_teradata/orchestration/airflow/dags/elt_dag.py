from pendulum import datetime
from airflow.decorators import dag
from airflow.providers.airbyte.operators.airbyte import AirbyteTriggerSyncOperator
from airflow.operators.trigger_dagrun import TriggerDagRunOperator

from airflow.operators.bash import BashOperator
from airflow.models import Variable
# Define the ELT DAG

dbt_dir = Variable.get("dbt_base_dir")

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
        trigger_dag_id="dbt_ecommerce",
        wait_for_completion=True,
        poke_interval=30,
    )


    # Set the order of tasks
    extract_data >> trigger_dbt_dag

# Instantiate the ELT DAG
extract_and_transform_dag = extract_and_transform()

# Define dbt transform
@dag(
    dag_id="dbt_ecommerce",
    start_date=datetime(2023, 10, 1),
    tags=["dbt", "teradata", "ecommerce"],
    catchup=False,
) 
def transform():

    transform_data = BashOperator(
        task_id="transform_data",
        bash_command="dbt run --profiles-dir {{ dbt_dir }} --project-dir {{ dbt_dir }} -s path:models/ecommerce"
    )
    
    transform_data


# Instantiate the dbt DAG
transform()
