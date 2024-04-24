select
    CAST (_airbyte_data.JSONExtractValue('$.id')  AS int)  as id,
    CAST (_airbyte_data.JSONExtractValue('$.gender')   AS VARCHAR(50)) as gender,
     CAST (_airbyte_data.JSONExtractValue('$.academic_degree')    AS VARCHAR(50))  as academic_degree,
     CAST (_airbyte_data.JSONExtractValue('$.title')  AS VARCHAR(200)) as mtitle,
	 CAST (_airbyte_data.JSONExtractValue('$.nationality')  AS VARCHAR(50)) as nationality,	 
	 CAST (_airbyte_data.JSONExtractValue('$.age')  AS int) as age,
	 CAST (_airbyte_data.JSONExtractValue('$.name')  AS VARCHAR(200)) as name,
	 CAST (_airbyte_data.JSONExtractValue('$.email')  AS VARCHAR(200)) as email,
	 CAST (_airbyte_data.JSONExtractValue('$.created_at')  AS timestamp) as created_at,
	 CAST (_airbyte_data.JSONExtractValue('$.updated_at')  AS timestamp) as updated_at,
    _airbyte_emitted_at as _airbyte_extracted_at
from {{ source('ecommerce_demo', '_airbyte_raw_users') }} as table_alias
