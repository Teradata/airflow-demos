select
    CAST(id AS int) as id,
    CAST(gender AS VARCHAR(50)) as gender,
    CAST(academic_degree AS VARCHAR(50)) as academic_degree,
	CAST("title" AS VARCHAR(200)) as _title,
	CAST(nationality AS VARCHAR(50)) as nationality,
	CAST(age AS int) as age,
	CAST(name AS VARCHAR(200)) as name,
	CAST(email AS VARCHAR(200)) as email,
	CAST(created_at AS timestamp) as created_at,
	CAST(updated_at AS timestamp) as updated_at,
    CAST(_airbyte_extracted_at AS timestamp) as _airbyte_extracted_at
from {{ source('sources_ecommerce', 'users') }} as table_alias
