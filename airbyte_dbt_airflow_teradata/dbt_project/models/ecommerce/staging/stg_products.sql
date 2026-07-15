select
    CAST(id AS int) as id,
    CAST("year" AS int) as _year,
    CAST(price AS decimal(18, 2)) as price,
    CAST(model AS VARCHAR(200)) as model,
	CAST(make AS VARCHAR(200)) as make,
	CAST(created_at AS timestamp) as created_at,
	CAST(updated_at AS timestamp) as updated_at,
    CAST(_airbyte_extracted_at AS timestamp) as _airbyte_extracted_at
from {{ source('sources_ecommerce', 'products') }} as table_alias
