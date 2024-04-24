select
    CAST (_airbyte_data.JSONExtractValue('$.id')  AS int)  as id,
    CAST (_airbyte_data.JSONExtractValue('$.year')   AS int) as myear,
     CAST (_airbyte_data.JSONExtractValue('$.price')    AS int)  as price,
     CAST (_airbyte_data.JSONExtractValue('$.model')  AS VARCHAR(200)) as model,
	 CAST (_airbyte_data.JSONExtractValue('$.make')  AS VARCHAR(200)) as make,
	 CAST (_airbyte_data.JSONExtractValue('$.created_at')  AS timestamp) as created_at,
	 CAST (_airbyte_data.JSONExtractValue('$.updated_at')  AS timestamp) as updated_at,
    _airbyte_emitted_at as _airbyte_extracted_at
from {{ source('ecommerce_demo', '_airbyte_raw_products') }} as table_alias
