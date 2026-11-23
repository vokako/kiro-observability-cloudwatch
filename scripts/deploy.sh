#!/usr/bin/env bash
# Create or update the Kiro observability stack, then print its links.
#
#   REGION=us-east-1 ./scripts/deploy.sh
#   REGION=us-east-1 ./scripts/deploy.sh ConfigureKiroPolicy=false        # any template parameter
#   REGION=us-east-1 STACK_NAME=kiro-observability ./scripts/deploy.sh
#
# On update, parameters you don't pass keep their current values.
set -euo pipefail
export AWS_PAGER=""
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
: "${REGION:?Set REGION to us-east-1 or eu-central-1 (the Region of your Kiro profile)}"
STACK_NAME="${STACK_NAME:-kiro-observability}"
TEMPLATE="$ROOT/template/kiro-observability.yaml"

python3 "$ROOT/src/build_template.py" template > "$TEMPLATE"
echo "Account $(aws sts get-caller-identity --query Account --output text), Region $REGION, stack $STACK_NAME"

args=(--region "$REGION" --stack-name "$STACK_NAME" --template-file "$TEMPLATE"
      --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset --tags purpose=kiro-otel)
if [ $# -gt 0 ]; then args+=(--parameter-overrides "$@"); fi
if ! aws cloudformation deploy "${args[@]}"; then
  echo "Deploy failed. Reasons:" >&2
  aws cloudformation describe-stack-events --region "$REGION" --stack-name "$STACK_NAME" --max-items 30 \
    --query 'StackEvents[?contains(ResourceStatus,`FAILED`)].[LogicalResourceId,ResourceStatusReason]' --output text >&2
  exit 1
fi

echo
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[?OutputKey!=`KglSettings`].[OutputKey,OutputValue]' --output text | column -t -s $'\t'
echo
echo "Real-time events arrive seconds after the next Kiro request; daily metrics after the next 00:30 UTC."
echo "Rotate the keys now: aws secretsmanager rotate-secret --region $REGION --secret-id <EventsSecretArn or MetricsSecretArn>"
echo "Remove everything:   aws cloudformation delete-stack --region $REGION --stack-name $STACK_NAME"
