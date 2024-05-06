SELECT 
  user_id,
  product_id,
  purchased_at,
  added_to_cart_at,  
  returned_at,
(purchased_at - added_to_cart_at) DAY(4) AS time_to_purchase_days
FROM {{ ref('stg_purchases') }}





