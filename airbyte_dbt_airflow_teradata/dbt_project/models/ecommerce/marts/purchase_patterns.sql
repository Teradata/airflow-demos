SELECT 
  user_id,
  product_id,
  purchased_at,
  added_to_cart_at,  
  returned_at
FROM {{ ref('stg_purchases') }}





