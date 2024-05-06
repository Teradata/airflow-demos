WITH base AS (
  SELECT 
    id AS user_id,
    gender,
    academic_degree,
    nationality,
    age
  FROM {{ ref('stg_customers') }}
)

SELECT 
  gender,
  academic_degree,
  nationality,
  AVG(age) AS average_age,
  COUNT(user_id) AS customer_count
FROM base
GROUP BY 1, 2, 3
