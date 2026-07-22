# 0006 - local-only server; cloud code lives elsewhere

Decision: Cognito auth, EC2 autostop/wake, AWS-Batch dispatch, DynamoDB,
SSM vault, and the GCP publish path were removed from this repo (the cloud
product keeps its own frozen copy). KEPT: boto3 (it is the MinIO client),
the Bedrock adapter (one option of the pluggable-LLM seam), and public-S3
data fetchers (product data layer, not infra).
