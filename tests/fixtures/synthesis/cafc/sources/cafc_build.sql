-- Sanitized CAFC SQL job (illustrative). Never executed by synthesis.
INSERT INTO analytics.customer_feature_composite_daily
SELECT
  c.customer_id AS customer_id,
  u.business_date AS business_date,
  sha2(c.msisdn, 256) AS msisdn_hash,
  AVG(u.revenue) OVER (
    PARTITION BY c.customer_id
    ORDER BY u.business_date
    ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
  ) AS arpu_30d,
  r.region_code AS region_code
FROM crm.customers c
JOIN billing.usage_daily u
  ON c.customer_id = u.customer_id
LEFT JOIN reference.dim_region r
  ON c.region_key = r.region_key
WHERE u.business_date = DATE '${biz_date}';
