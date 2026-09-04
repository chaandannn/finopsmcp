#!/usr/bin/env bash
# A $300/month cap on nable's OWN GCP spend that actually stops, not just warns.
#
#   gcloud auth login && gcloud auth application-default login
#   scripts/gcp/budget-kill.sh
#
# WHY THIS IS NOT JUST A BUDGET. A GCP budget sends notifications. It has no
# power to stop anything, so a runaway job bills right through 100% and keeps
# going. The only mechanism that actually halts spend is detaching the billing
# account from the project, and the only way to do that automatically is a
# function subscribed to the budget's Pub/Sub topic.
#
# WHAT DETACHING DOES. Everything on the project stops: Cloud Run stops serving,
# Cloud SQL stops. Data is not deleted, and re-attaching billing brings it back,
# but it is an outage, deliberately, because that is what a cap is.
#
# SO THE TWO PROJECTS ARE TREATED DIFFERENTLY, on purpose:
#   nable-dev   hard kill at 100%. Nothing here is worth a surprise bill.
#   nable-prod  alerts only. Once a customer is on it, an automatic outage costs
#               more than the overage it prevents. Flip PROD_HARD_KILL=1 if you
#               disagree, and know that you are choosing downtime over spend.
set -euo pipefail

export PATH="/usr/local/share/google-cloud-sdk/bin:$PATH"
BUDGET_USD="${BUDGET_USD:-300}"
DEV_PROJECT="${DEV_PROJECT:-nable-dev}"
PROD_PROJECT="${PROD_PROJECT:-nable-prod}"
PROD_HARD_KILL="${PROD_HARD_KILL:-0}"
REGION="${REGION:-us-central1}"

BILLING=$(gcloud beta billing projects describe "$DEV_PROJECT" \
          --format='value(billingAccountName)' | sed 's|billingAccounts/||')
[ -n "$BILLING" ] || { echo "could not find the billing account"; exit 1; }
echo "billing account: $BILLING"
echo "cap: \$${BUDGET_USD}/mo   hard kill: $DEV_PROJECT$([ "$PROD_HARD_KILL" = 1 ] && echo " + $PROD_PROJECT")"

TOPIC="nable-budget-kill"
gcloud pubsub topics create "$TOPIC" --project "$DEV_PROJECT" 2>/dev/null \
  || echo "topic exists"

# The function. Detaches billing only when actual cost has crossed the cap, and
# only for projects on the kill list: a budget notification fires at every
# threshold, and acting on the 50% one would take the project down at half price.
WORK=$(mktemp -d)
cat > "$WORK/main.py" <<'PY'
import base64, json, os
from googleapiclient import discovery

KILL = [p.strip() for p in os.environ.get("KILL_PROJECTS", "").split(",") if p.strip()]

def kill(event, context):
    msg = json.loads(base64.b64decode(event["data"]).decode())
    cost, budget = float(msg.get("costAmount", 0)), float(msg.get("budgetAmount", 0))
    if not budget or cost < budget:
        print(f"under cap: {cost:.2f}/{budget:.2f}")
        return
    billing = discovery.build("cloudbilling", "v1", cache_discovery=False)
    for project in KILL:
        name = f"projects/{project}"
        info = billing.projects().getBillingInfo(name=name).execute()
        if not info.get("billingEnabled"):
            print(f"{project}: billing already off")
            continue
        # The kill. An empty billingAccountName detaches the project.
        billing.projects().updateBillingInfo(
            name=name, body={"billingAccountName": ""}).execute()
        print(f"{project}: BILLING DISABLED at ${cost:.2f} of ${budget:.2f}")
PY
cat > "$WORK/requirements.txt" <<'PY'
google-api-python-client==2.*
PY

echo "deploying the kill function..."
gcloud functions deploy nable-budget-kill \
  --project "$DEV_PROJECT" --region "$REGION" \
  --runtime python312 --entry-point kill --source "$WORK" \
  --trigger-topic "$TOPIC" --no-allow-unauthenticated \
  --set-env-vars "KILL_PROJECTS=$DEV_PROJECT$([ "$PROD_HARD_KILL" = 1 ] && echo ",$PROD_PROJECT")" \
  --quiet

# The function needs power over billing, and this is the one grant that makes the
# kill possible. It is also the grant that makes the function dangerous, so it is
# scoped to the billing account and held by nothing else.
SA=$(gcloud functions describe nable-budget-kill --project "$DEV_PROJECT" \
     --region "$REGION" --format='value(serviceConfig.serviceAccountEmail)')
gcloud beta billing accounts add-iam-policy-binding "$BILLING" \
  --member "serviceAccount:$SA" --role roles/billing.projectManager --quiet >/dev/null
echo "granted billing.projectManager to $SA"

# The budget itself: alerts on the way up, Pub/Sub at every threshold so the
# function can decide. Replaces the $100 budget set on 2026-08-22.
for existing in $(gcloud billing budgets list --billing-account "$BILLING" \
                  --format='value(name)' 2>/dev/null); do
  gcloud billing budgets delete "$existing" --quiet 2>/dev/null || true
done
gcloud billing budgets create --billing-account "$BILLING" \
  --display-name "nable internal cap (\$${BUDGET_USD}/mo, hard kill)" \
  --budget-amount "${BUDGET_USD}USD" \
  --threshold-rule percent=0.5 \
  --threshold-rule percent=0.9 \
  --threshold-rule percent=1.0 \
  --threshold-rule percent=1.0,basis=forecasted-spend \
  --notifications-rule-pubsub-topic "projects/$DEV_PROJECT/topics/$TOPIC" \
  --quiet

rm -rf "$WORK"
echo
echo "done. \$${BUDGET_USD}/mo cap is live."
echo "  $DEV_PROJECT   billing detaches automatically at 100%"
echo "  $PROD_PROJECT  $([ "$PROD_HARD_KILL" = 1 ] && echo "billing detaches automatically at 100%" || echo "alerts only, no automatic outage")"
echo
echo "To undo a kill:  gcloud beta billing projects link <project> --billing-account $BILLING"
