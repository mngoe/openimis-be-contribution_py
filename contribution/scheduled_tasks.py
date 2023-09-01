from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from contribution.models import Premium
from contribution.gql_mutations import get_access_token
from policy.models import Policy
import json, requests
import os

def get_status(premium, access_token):
    key = os.environ.get("OM_KEY")
    secret = os.environ.get("OM_SECRET")
    x_auth = os.environ.get("X_AUTH_TOKEN")
    if not premium.paytoken:
        return False
    statut_url = "https://omapi.ynote.africa/dev/webpayment/status/"+premium.paytoken
    payload = {
        "customer_key": key,
        "customer_secret": secret,
        "x_auth_token": x_auth + "="
    }
    headers = {
        'Authorization': 'Bearer '+ access_token,
        'Content-Type': 'application/json'
    }
    response = requests.request(
        "GET",
        statut_url,
        headers=headers,
        data=json.dumps(payload)
    )
    statut_rep = False
    try:
        statut_rep = response.json()
    except Exception as e:
        ("Exection on response check: ", statut_rep)
    return statut_rep


def merchand_payment_task():
    print("Starting MP request cron...")
    request_token = get_access_token()
    if request_token:
        if isinstance(request_token, dict):
            access_token = request_token.get('access_token', False)
            premiums = Premium.objects.filter(cron_treated=False).filter(network_operator='O').filter(paytoken__isnull=False)
            print("premiums: ", premiums)
            if access_token:
                for premium in premiums:
                    statut_rep = get_status(premium, access_token)
                    if isinstance(statut_rep, dict):
                        if "data" in statut_rep:
                            if "status" in statut_rep["data"]:
                                if statut_rep["data"]["status"] == "SUCCESSFULL":
                                    total = premium.amount
                                    premium.cron_treated = 1
                                    premium.save()
                                    print("valeur police ", premium.policy.value)
                                    other_premiums = Premium.objects.filter(policy_id=premium.policy.id).exclude(id=premium.id).all()
                                    print("other_premiums ", other_premiums)
                                    for other_premium in other_premiums:
                                        if other_premium.network_operator == 'O':
                                            result_status = get_status(other_premium, access_token)
                                            if isinstance(result_status, dict):
                                                if "data" in result_status:
                                                    if "status" in result_status["data"]:
                                                        if result_status["data"]["status"] == "SUCCESSFULL":
                                                            total += other_premium.amount
                                                            other_premium.cron_treated = 1
                                                            other_premium.save()
                                        else:
                                            total += other_premium.amount
                                    if total >= premium.policy.value:
                                        premium.policy.status=Policy.STATUS_ACTIVE
                                        premium.policy.save()
    else:
        print('Unable to get access token', request_token)
    print("End MP request cron...")
    return True

def schedule_tasks(scheduler: BackgroundScheduler):
    scheduler.add_job(
        merchand_payment_task,
        trigger=CronTrigger(minute="*"),
        id="om_merchand_payment_check",
        max_instances=1,
        replace_existing=True
    )