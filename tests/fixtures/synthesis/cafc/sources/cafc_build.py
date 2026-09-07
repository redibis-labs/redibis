"""Sanitized CAFC Spark job (illustrative). Parsed via AST only — never imported/executed."""

from pyspark.sql import SparkSession
from pyspark.sql import functions as F


def build_cafc(spark: SparkSession, biz_date: str):
    customers = spark.table("crm.customers")
    usage = spark.table("billing.usage_daily").filter(F.col("business_date") == biz_date)
    regions = spark.table("reference.dim_region")

    joined = (
        customers.alias("c")
        .join(usage.alias("u"), on="customer_id", how="inner")
        .join(
            regions.alias("r"),
            F.col("c.region_key") == F.col("r.region_key"),
            how="left",
        )
    )

    out = joined.select(
        F.col("c.customer_id").alias("customer_id"),
        F.col("u.business_date").alias("business_date"),
        F.sha2(F.col("c.msisdn"), 256).alias("msisdn_hash"),
        F.col("u.revenue").alias("arpu_30d"),
        F.col("r.region_code").alias("region_code"),
    )

    (
        out.write.mode("overwrite")
        .partitionBy("business_date")
        .saveAsTable("analytics.customer_feature_composite_daily")
    )
    return out
