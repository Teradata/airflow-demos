SELECT 
    CAST(id AS int) as id,
    CAST(user_id AS int) as user_id,
    CAST(product_id AS int) as product_id,
     CASE 
        WHEN purchased_at IS NULL OR purchased_at = 'None' THEN null
        ELSE CAST(purchased_at AS timestamp)
    END AS purchased_at,
    CASE 
        WHEN returned_at IS NULL OR returned_at = 'None' THEN null
        ELSE CAST(returned_at AS timestamp)
    END AS returned_at,
    CAST(created_at AS timestamp) as created_at,
    CAST(updated_at AS timestamp) as updated_at,
    CAST(added_to_cart_at AS timestamp) as added_to_cart_at,
    CAST(_airbyte_extracted_at AS timestamp) as _airbyte_extracted_at
from {{ source('sources_ecommerce', 'purchases') }} as table_alias
