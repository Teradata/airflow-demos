from cosmos.config import ProjectConfig, ProfileConfig
from cosmos.profiles import TeradataUserPasswordProfileMapping


project_config = ProjectConfig(
    dbt_project_path="/opt/airflow/dbt_project",
)

profile = TeradataUserPasswordProfileMapping(
            conn_id = 'teradata_connection'
)


profile_config = ProfileConfig(
    profile_name="dbt_project",
    target_name="dev",
    profile_mapping=profile
)
