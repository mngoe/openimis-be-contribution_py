from contribution.services import premium_updated
from policy.models import Policy
from typing import Optional

import graphene
from contribution.apps import ContributionConfig
from contribution.models import Premium, PremiumMutation
from payer.models import Payer
from policy import models as policy_models
from core.schema import OpenIMISMutation
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext as _
from core import datetime
import logging

logger = logging.getLogger(__name__)


class PremiumBase:
    """
    This takes most parameters of the Premium with addition of action. This fields allows to force
    """
    id = graphene.Int(required=False, read_only=True)
    uuid = graphene.String(required=False)
    policy_uuid = graphene.String(required=True)
    payer_uuid = graphene.String()
    amount = graphene.Decimal()
    receipt = graphene.String()
    pay_date = graphene.Date()
    pay_type = graphene.String(max_length=1)
    is_offline = graphene.Boolean(required=False)
    is_photo_fee = graphene.Boolean(required=False)
    action = graphene.String(required=False)
    # json_ext = graphene.types.json.JSONString(required=False)
    payment_number = graphene.String(required=True)
    network_operator = graphene.String(required=True)


def reset_premium_before_update(premium):
    premium.amount = None
    premium.receipt = None
    premium.policy = None
    premium.payer = None
    premium.pay_date = None
    premium.pay_type = None
    premium.is_photo_fee = None
    premium.is_offline = None
    premium.reporting_id = None
    premium.payment_number = None
    premium.network_operator = None


def update_or_create_premium(data, user):
    if "client_mutation_id" in data:
        data.pop('client_mutation_id')
    if "client_mutation_label" in data:
        data.pop('client_mutation_label')
    now = datetime.datetime.now()
    data['audit_user_id'] = user.id_for_audit
    data['validity_from'] = now
    policy_uuid = data.pop("policy_uuid") if "policy_uuid" in data else None
    if not policy_uuid:
        raise Exception(_("policy_uuid_required"))
    policy = Policy.filter_queryset(None).filter(uuid=policy_uuid).first()
    if not policy:
        raise Exception(_("policy_uuid_not_found") % (policy_uuid,))
    data["policy"] = policy
    # TODO verify that the user has access to specified payer_id
    premium_uuid = data.pop("uuid") if "uuid" in data else None
    # action: enforce, suspend, wait
    action = data.pop("action") if "action" in data else None
    payer_uuid = data.pop("payer_uuid") if "payer_uuid" in data else None
    payer = Payer.filter_queryset().filter(uuid=payer_uuid).first() if payer_uuid else None
    if premium_uuid:
        premium = Premium.objects.get(uuid=premium_uuid)
        premium.save_history()
        reset_premium_before_update(premium)
        [setattr(premium, k, v) for k, v in data.items()]

        if payer_uuid and payer:
            premium.payer = payer
        premium.save()
    else:
        premium = Premium.objects.create(**data)
    if payer_uuid and payer:
        premium.payer = payer
        premium.save()
    # Handle the policy updating
    premium_updated(premium, action)
    return premium


class CreatePremiumMutation(OpenIMISMutation):
    """
    Create a contribution for policy with or without a payer
    """
    _mutation_module = "contribution"
    _mutation_class = "CreatePremiumMutation"

    class Input(PremiumBase, OpenIMISMutation.Input):
        pass

    @classmethod
    def async_mutate(cls, user, **data) -> Optional[str]:
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(
                    _("mutation.authentication_required"))
            if not user.has_perms(ContributionConfig.gql_mutation_create_premiums_perms):
                raise PermissionDenied(_("unauthorized"))
            client_mutation_id = data.get("client_mutation_id")
            premium = update_or_create_premium(data, user)
            PremiumMutation.object_mutated(user, client_mutation_id=client_mutation_id, premium=premium)
            return None
        except Exception as exc:
            return [{
                'message': _("contribution.mutation.failed_to_create_premium"),
                'detail': str(exc)}
            ]


class UpdatePremiumMutation(OpenIMISMutation):
    """
    Update a contribution for policy with or without a payer
    """
    _mutation_module = "contribution"
    _mutation_class = "UpdatePremiumMutation"

    class Input(PremiumBase, OpenIMISMutation.Input):
        pass

    @classmethod
    def async_mutate(cls, user, **data) -> Optional[str]:
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(
                    _("mutation.authentication_required"))
            if not user.has_perms(ContributionConfig.gql_mutation_update_premiums_perms):
                raise PermissionDenied(_("unauthorized"))
            update_or_create_premium(data, user)
            return None
        except Exception as exc:
            return [{
                'message': _("contribution.mutation.failed_to_update_premium") %
                           {'id': data.get('id') if data else None},
                'detail': str(exc)}
            ]


class DeletePremiumsMutation(OpenIMISMutation):
    """
    Delete one or several Premiums.
    """
    _mutation_module = "contribution"
    _mutation_class = "DeletePremiumsMutation"

    class Input(OpenIMISMutation.Input):
        uuids = graphene.List(graphene.String)

    @classmethod
    def async_mutate(cls, user, **data):
        if not user.has_perms(ContributionConfig.gql_mutation_delete_premiums_perms):
            raise PermissionDenied(_("unauthorized"))
        errors = []
        for premium_uuid in data["uuids"]:
            premium = Premium.objects \
                .filter(uuid=premium_uuid) \
                .first()
            if premium is None:
                errors.append({
                    'title': premium_uuid,
                    'list': [{'message': _(
                        "contribution.validation.id_does_not_exist") % {'id': premium_uuid}}]
                })
                continue
            errors += set_premium_deleted(premium)
        if len(errors) == 1:
            errors = errors[0]['list']
        return errors


def set_premium_deleted(premium):
    try:
        premium.delete_history()
        return []
    except Exception as exc:
        logger.debug("Exception when deleting premium %s", premium.uuid, exc_info=exc)
        return {
            'title': premium.uuid,
            'list': [{
                'message': _("contribution.mutation.failed_to_delete_premium") % {'uuid': premium.uuid},
                'detail': premium.uuid}]
        }


def on_policy_mutation(sender, **kwargs):
    errors = []
    if kwargs.get("mutation_class") == 'DeletePoliciesMutation':
        uuids = kwargs['data'].get('uuids', [])
        policies = policy_models.Policy.objects.prefetch_related("premiums").filter(uuid__in=uuids).all()
        for policy in policies:
            for premium in policy.premiums.all():
                errors += set_premium_deleted(premium)
    return errors


def on_premium_mutation(sender, **kwargs):
    uuids = kwargs['data'].get('uuids', [])
    if not uuids:
        uuid = kwargs['data'].get('uuid', None)
        uuids = [uuid] if uuid else []
    if not uuids:
        return []
    impacted_premiums = Premium.objects.filter(uuid__in=uuids).all()
    for premium in impacted_premiums:
        PremiumMutation.objects.update_or_create(premium=premium, mutation_id=kwargs['mutation_log_id'])
    return []
