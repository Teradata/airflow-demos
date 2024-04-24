select
    CAST (_airbyte_data.JSONExtractValue('$.id')  AS int)  as id,
    CAST (_airbyte_data.JSONExtractValue('$.user_id')   AS int) as user_id,
     CAST (_airbyte_data.JSONExtractValue('$.product_id')    AS int)  as product_id,
     CAST (_airbyte_data.JSONExtractValue('$.purchased_at')  AS timestamp) as purchased_at,
	 CAST (_airbyte_data.JSONExtractValue('$.returned_at')  AS timestamp) as returned_at,
	 CAST (_airbyte_data.JSONExtractValue('$.created_at')  AS timestamp) as created_at,
	 CAST (_airbyte_data.JSONExtractValue('$.updated_at')  AS timestamp) as updated_at,
	 CAST (_airbyte_data.JSONExtractValue('$.added_to_cart_at')  AS timestamp) as added_to_cart_at,
    _airbyte_emitted_at as _airbyte_extracted_at
from {{ source('ecommerce_demo', '_airbyte_raw_purchases') }} as table_alias
