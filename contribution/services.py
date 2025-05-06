import logging
from enum import Enum
from core.models import filter_validity
from core.datetimes.shared import datetimedelta
from django.db.models import Sum, Q
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from django.db.transaction import atomic
from insuree.models import Insuree, Family, InsureePolicy
from location.apps import LocationConfig
from location.models import Location
from policy.models import Policy
from policy.services import policy_status_premium_paid
from contribution.apps import ContributionConfig
from datetime import timedelta, datetime as py_datetime
from invoice.services import InvoiceService
from invoice.services.invoiceLineItem import InvoiceLineItemService
from invoice.models import Invoice
from .models import Premium, PayTypeChoices
from policy.apps import CALCULATION_RULES
from contribution_plan.models import ContributionPlan
from decimal import Decimal

logger = logging.getLogger(__name__)

# A fake family is used for funding
FUNDING_CHF_ID = "999999999"


class ByPolicyPremiumsAmountService(object):
    def __init__(self, user):
        self.user = user

    def request(self, policy_id):
        return (
            Premium.objects.filter(policy_id=policy_id)
            .exclude(is_photo_fee=True)
            .aggregate(Sum("amount"))["amount__sum"]
        )


def last_date_for_payment(policy_id):
    policy = Policy.objects.get(id=policy_id)
    has_cycle = policy.product.has_cycle()

    if policy.stage == "N":
        grace_period = policy.product.grace_period_enrolment
    elif policy.stage == "R":
        grace_period = policy.product.grace_period_renewal
    else:
        logger.error(
            "policy stage should be either N or R, policy %s has %s",
            policy_id,
            policy.stage,
        )
        raise Exception("policy stage should be either N or R")

    waiting_period = policy.product.grace_period_payment

    if has_cycle:
        # Calculate on fixed cycle
        start_date = policy.start_date

        last_date = start_date + datetimedelta(months=grace_period)
    else:
        # Calculate on Free Cycle
        if policy.stage == "N":
            last_date = policy.enroll_date + datetimedelta(months=waiting_period)
        else:
            last_date = (
                policy.expiry_date
                + datetimedelta(days=1)
                + datetimedelta(months=waiting_period)
            )

    return last_date - datetimedelta(days=1)


def can_payer_fund_product(payer, product):
    if not product.location:
        return True
    elif product.location == payer.location:
        return True
    elif product.location.type == "R" and payer.location.parent == product.location:
        return True
    elif product.location.type == "D" and payer.location == product.location.parent:
        return True

    return False


@atomic
def add_fund(payer, product, pay_date, amount, receipt, audit_user_id, is_offline):
    # We create fake locations for fundings
    fundings = []
    funding_parent = None  # Top Funding has no parent, then the loop will chain them
    for level in LocationConfig.location_types:
        level_funding, funding_created = Location.objects.get_or_create(
            code=f"F{level}",
            name="Funding",
            parent=funding_parent,
            type=level,
            defaults=dict(audit_user_id=audit_user_id),
        )
        funding_parent = level_funding
        fundings.append(level_funding)
        if funding_created:
            logger.warning("Created funding at level %s", level)

    if product.validity_to is not None:
        raise ValueError("Product has to be valid")

    if not can_payer_fund_product(payer, product):
        raise ValueError("Payer and product locations are incompatible")

    # TODO check and/or document premium_adult
    product_value = product.lump_sum or product.premium_adult

    # Check if the family with CHFID exists
    # Original procedure here has a strange and useless join on isnull(,0), ignoring
    family = (
        Family.objects.filter(validity_to__isnull=True)
        .filter(head_insuree__chf_id=FUNDING_CHF_ID)
        .filter(location_id=product.location_id)
        .first()
    )

    if not family:
        insuree = Insuree.objects.create(
            family=family,
            chf_id=FUNDING_CHF_ID,
            last_name="Funding",
            other_names="Funding",
            gender=None,
            marital=None,
            head=True,
            card_issued=False,
            dob=pay_date,
            audit_user_id=audit_user_id,
            offline=is_offline,
        )
        family = Family.objects.create(
            head_insuree=insuree,
            location_id=product.location_id,
            poverty=False,
            is_offline=is_offline,
            audit_user_id=audit_user_id,
        )

    from core import datetimedelta

    policy = Policy.objects.create(
        family=family,
        enroll_date=pay_date,
        start_date=pay_date,
        effective_date=pay_date,
        expiry_date=datetimedelta(months=product.insurance_period).add_to_date(
            pay_date
        ),
        status=Policy.STATUS_ACTIVE,
        value=product_value,
        product=product,
        officer_id=None,
        audit_user_id=audit_user_id,
        offline=is_offline,
    )

    InsureePolicy.objects.create(
        insuree=family.head_insuree,
        policy=policy,
        enrollment_date=policy.enroll_date,
        start_date=policy.start_date,
        effective_date=policy.effective_date,
        expiry_date=policy.expiry_date,
        audit_user_id=audit_user_id,
        offline=is_offline,
    )

    return Premium.objects.create(
        policy=policy,
        payer_id=payer.id,
        amount=amount,
        receipt=receipt,
        pay_date=pay_date,
        pay_type=PayTypeChoices.FUNDING,
        is_offline=is_offline,
        audit_user_id=audit_user_id,
    )
from django.db.models import Q


class PremiumUpdateActionEnum(Enum):
    SUSPEND = "SUSPEND"
    ENFORCE = "ENFORCE"
    WAIT = "WAIT"


def premium_updated(premium, action=None):
    """
    if the contribution is lower than the policy value, action can override it or suspend the policy
    if it is right or too much, just activate it (enforce is still expected but just a warning)
    """
    policy = premium.policy
    policy.save_history()

    if action == PremiumUpdateActionEnum.SUSPEND.value:
        policy.status = Policy.STATUS_SUSPENDED
        policy.save()
        return

    policy_balance = policy.value - premium.other_premiums()
    
    if premium.amount  == policy_balance:
        policy_status_premium_paid(
            policy,
            premium.pay_date
            if premium.pay_date > policy.start_date
            else policy.start_date,
        )
    elif premium.amount < policy_balance:
        # suspend already handledpremium
        if action == PremiumUpdateActionEnum.ENFORCE.value:
            policy_status_premium_paid(policy, premium.pay_date)
        # otherwise, just leave the policy unchanged
    elif premium.amount > policy_balance:
        if action != PremiumUpdateActionEnum.ENFORCE.value:
            logger.warning("action on premiums larger than the policy value")
        policy_status_premium_paid(policy, premium.pay_date)
    else:
        logger.warning(
            "The comparison between premium amount %s and policy value %s failed",
            premium.amount,
            policy.value,
        )
        raise Exception("Invalid combination or premium and policy amounts")

    if policy.status is not None and (
        policy.effective_date == premium.pay_date
        or policy.effective_date == policy.start_date
    ):
        # Enforcing policy
        if policy.offline or not premium.is_offline:
            policy.save()
        if policy.status == Policy.STATUS_ACTIVE:
            _update_policy_insurees(policy)
    elif policy.effective_date:
        _activate_insurees(policy, premium.pay_date)


def _update_policy_insurees(policy):
    policy.insuree_policies.filter(validity_to__isnull=True).update(
        effective_date=policy.effective_date,
        start_date=policy.start_date,
        expiry_date=policy.expiry_date,
    )


def _activate_insurees(policy, pay_date):
    policy.insuree_policies.filter(validity_to__isnull=True).update(
        effective_date=pay_date,
    )


def check_unique_premium_receipt_code_within_product(code, policy_uuid = None, policy = None):
    from .models import Premium

    if not policy:
        if not policy_uuid:
            return [{"message": "missing Policy"}]
        policy = Policy.objects.select_related('product').filter(uuid=policy_uuid, validity_to__isnull=True).first()
    exists = Premium.objects.filter(policy__product=policy.product, receipt=code, validity_to__isnull=True).exists()
    if exists:
        return [{"message": "Premium code %s already exists" % code}]
    return []


def update_or_create_premium(premium, user, action=None):
    existing_premium = Premium.objects.filter(*filter_validity(), Q(Q(uuid=premium.uuid) | Q(id=premium.id))).first()
    if existing_premium:
        return update_premium(existing_premium, premium, user, action)
    else:
        value_return = create_premium(premium, user, action)
        logger.warning("Config for invoice generation %s",
                       ContributionConfig.generate_invoice_on_contribution)
        if ContributionConfig.generate_invoice_on_contribution:
            family_amount = 0
            government_amount = 0
            instance = ContributionPlan.objects.filter(
                uuid=str(
                    premium.policy.contribution_plan.id
                )
            )
            instance = instance[0]
            if instance:
                for calculation_rule in CALCULATION_RULES:
                    # get calculation_rule amount for government
                    result_signal = calculation_rule.signal_calculate_event.send(
                        sender=instance.__class__.__name__, instance=instance,
                        user=user, context="create",
                        family=premium.policy.family,
                        is_government_value=True
                    )
                    logger.warning("result_signal %s ", result_signal)
                    if result_signal[0][1]:
                        government_amount = Decimal(result_signal[0][1])
                        logger.warning("government_amount %s ", government_amount)
                    
                    # get calculation_rule for familly
                    result_signal = calculation_rule.signal_calculate_event.send(
                        sender=instance.__class__.__name__, instance=instance,
                        user=user, context="create",
                        family=premium.policy.family,
                        is_government_value=False
                    )
                    logger.warning("result_signal2 %s ", result_signal)
                    if result_signal[0][1]:
                        family_amount = Decimal(result_signal[0][1])
                        logger.warning("family_amount %s ", family_amount)
            if premium.policy.contribution_plan:
                logger.warning("date_valid_from of the contribution %s",
                               premium.policy.contribution_plan.date_valid_from)
                logger.warning("date_valid_to of the contribution %s",
                               premium.policy.contribution_plan.date_valid_to)
                today = py_datetime.now()
                generate = False
                if today > premium.policy.contribution_plan.date_valid_from:
                    if premium.policy.contribution_plan.date_valid_to:
                        if premium.policy.contribution_plan.date_valid_to > today:
                            generate = True
                    else:
                        # Validity to is null
                        generate = True
                if generate:
                    periodicity = 12
                    if premium.policy.periodicity:
                        if premium.policy.periodicity == 'Q':
                            periodicity = 3
                        elif premium.policy.periodicity == 'S':
                            periodicity = 6
                        elif premium.policy.periodicity == 'M':
                            periodicity = 1
                    renewal_date = today + datetimedelta(
                        months=periodicity
                    )
                    logger.warning("renewal date %s", renewal_date)
                    ok = False
                    if not premium.policy.contribution_plan.date_valid_to:
                        ok = True
                    else:
                        if renewal_date < premium.policy.contribution_plan.\
                            date_valid_to:
                            ok = True
                    if ok:
                        logger.warning("Family %s", premium.policy.family.id)
                        insuree_numbers = ""
                        members = Insuree.objects.filter(
                            family_id=premium.policy.family.id,
                            validity_to__isnull=True
                        )
                        for membre in members:
                            insuree_numbers += str(membre.id)
                        code = insuree_numbers + str(today.year) + str(today.month)
                        date_due = today + datetimedelta(
                            months=1
                        )
                        logger.warning("date due %s", date_due)
                        if premium.policy.payment_day:
                            date_due = date_due.replace(day=int(premium.policy.payment_day))
                            logger.warning("date due updated %s", date_due)
                        date_valid_to = renewal_date - timedelta(days=1)
                        logger.warning("current date_valid_to %s", date_valid_to)
                        quantity = 1
                        if premium.policy.periodicity:
                            if premium.policy.periodicity == 'Q':
                                family_amount = family_amount / 3
                                government_amount = government_amount / 3
                            elif premium.policy.periodicity == 'S':
                                family_amount = family_amount / 6
                                government_amount = government_amount / 6
                            elif premium.policy.periodicity == 'Y':
                                family_amount = family_amount / 12
                                government_amount = government_amount / 12
                        logger.warning("government amount %s ",
                                        government_amount)
                        logger.warning("family amount %s ", family_amount)
                        logger.warning("head insuree %s ",
                                            premium.policy.family.head_insuree)
                        existing_invoices = Invoice.objects.filter(
                            code=code)
                        if existing_invoices:
                            code = code + "_" + str(
                                len(existing_invoices)+1)
                        # create goverment invoice
                        if government_amount > 0:
                            values = {
                                "code": code,
                                "date_due": date_due,
                                "date_valid_from": date_due,
                                "date_valid_to": date_valid_to,
                                "amount_net": government_amount,
                                "amount_total": government_amount,
                                "status": 1,
                                "cron_job_code": code
                            }
                            if premium.policy.family.head_insuree:
                                values["subject_id"] = premium.policy.\
                                    family.head_insuree.id
                                values["subject_type"] = "insuree"
                                values["thirdparty_id"] = premium.policy.\
                                    family.head_insuree.id
                                values["thirdparty_type"] = "insuree"
                                if family_amount > 0:
                                    # update code as two invoice will be
                                    # created as the code is unique
                                    values["code"] = values["code"] + "-G"
                                    values["cron_job_code"] = values["cron_job_code"] + "-G"
                            invoice_service = InvoiceService(user=user)
                            result_invoice = invoice_service.create(
                                values
                            )
                            logger.warning(
                                "Invoice government_amount created %s",
                                result_invoice)
                            if result_invoice["success"] is True:
                                invoice_line_item_service =\
                                    InvoiceLineItemService(user=user)
                                item_values = {
                                    "invoice_id": result_invoice["data"]["id"],
                                    "code": code,
                                    "ledger_account": "Etat",
                                    "quantity": quantity,
                                    "unit_price": government_amount,
                                    "amount_net": government_amount,
                                    "amount_total": government_amount,
                                    "cron_job_code": code
                                }
                                if family_amount > 0:
                                    # update code as two invoice will be
                                    # created as the code is unique
                                    item_values["code"] = item_values["code"] + "-G"
                                    item_values["cron_job_code"] = item_values["cron_job_code"] + "-G"
                                result = invoice_line_item_service.create(
                                    item_values
                                )
                                logger.warning(
                                    "Invoice line gov_amount created %s",
                                    result)
                        # create Family invoice
                        if family_amount > 0:
                            invoice_service = InvoiceService(user=user)
                            gov_values = {
                                "code": code,
                                "date_due": date_due,
                                "date_valid_from": date_due,
                                "date_valid_to": date_valid_to,
                                "amount_net": family_amount,
                                "amount_total": family_amount,
                                "status": 1,
                                "cron_job_code": code
                            }
                            if premium.policy.family.head_insuree:
                                gov_values["subject_id"] = premium.policy.\
                                    family.head_insuree.id
                                gov_values["subject_type"] = "insuree"
                                gov_values["thirdparty_id"] = premium.policy.\
                                    family.head_insuree.id
                                gov_values["thirdparty_type"] = "insuree"
                            result_invoice = invoice_service.create(
                                gov_values
                            )
                            logger.warning(
                                "Invoice family amount created %s",
                                result_invoice)
                            if result_invoice["success"] is True:
                                invoice_line_item_service =\
                                    InvoiceLineItemService(user=user)
                                result = invoice_line_item_service.create(
                                    {
                                        "invoice_id": result_invoice["data"]["id"],
                                        "code": code,
                                        "ledger_account": "Cotisant",
                                        "quantity": quantity,
                                        "unit_price": family_amount,
                                        "amount_net": family_amount,
                                        "amount_total": family_amount,
                                        "cron_job_code": code
                                    }
                                )
                                logger.warning(
                                    "Invoice line amount_family created %s",
                                    result)
        return value_return


def update_premium(existing_premium, premium, user, action = None):
    if existing_premium.receipt != premium.receipt:
        if check_unique_premium_receipt_code_within_product(code=premium.receipt, policy=premium.policy):
            raise ValidationError(_("mutation.code_already_taken"))
    existing_premium.save_history()
    premium.id = existing_premium.id
    premium.save()
    # Handle the policy updating
    premium_updated(premium, action)
    return premium


def create_premium(premium, user, action = None):
    if check_unique_premium_receipt_code_within_product(code=premium.receipt, policy=premium.policy):
        raise ValidationError(_("mutation.code_already_taken"))
    premium.save()
    # Handle the policy updating
    premium_updated(premium, action)
    return premium